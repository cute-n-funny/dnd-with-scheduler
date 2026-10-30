# Scheduling UX and vote-rule upgrade

This patch applies to your existing bot **after** the private-channel, one-minute `/once_scheduler`, Windows-test, and Discord UX upgrades. It renames that test command to `/test_scheduler`.

## Features

- **Batch availability:** In the public poll, open **Set My Availability**, choose **multiple dates** and **multiple times**, then press **Add Times to Dates**. Every selected time is added to every selected date. Repeat to build your draft, use **Remove These Times** or **Clear Selected Dates** to adjust, and click **Save Availability** to save atomically. Other previously selected dates are preserved. Discord allows up to 25 options per select; the scheduler currently offers at most 16 dates and 12 times.
- **Soonest qualified session:** A slot qualifies when at least `attendee_limit` *distinct users* voted for that **exact date and time**. The earliest qualifying slot wins even when a later slot has more votes. Previously configured DM-voted-day restrictions are honored. If none qualifies, the existing most-popular-day / most-popular-time method (with random ties) is used. Users voting for multiple times on the same day count once in that day's total.
- **Default 8:** Newly started schedules default to eight attendee spots. Existing schedules keep the limit stored when the current schedule was created. To use 8, start a new schedule or explicitly set `attendee_limit:8`; `/reset_scheduler` keeps saved settings.
- **Vote carry-over:** Recurring polls automatically copy saved votes from the immediately preceding poll **only for exact future date-and-time slots that also appear in the new poll**. This can preserve votes from later weeks originally offered. The database primary key ensures votes are not duplicated and timestamps for RSVP priority are retained. Users may edit/cancel carried votes. Copies do not occur when starting a new schedule manually or in test mode. There is no invented weekday recurrence: only the same calendar date/time carries over. Votes for dates no longer offered are not copied.
- **Renamed test:** `/once_scheduler` becomes `/test_scheduler` with the same 1-minute silent poll, 1-minute delayed Test Event, 1-minute duration, no reminder, and no repeat.

## Apply and test (PowerShell)

```powershell
cd C:\Coding\dnd-with-scheduler
git apply --check "$HOME\Downloads\dnd-scheduler-availability-and-earliest-date.patch"
git apply "$HOME\Downloads\dnd-scheduler-availability-and-earliest-date.patch"
python -m unittest discover -s tests -v
python -m compileall -q bot.py logic.py storage.py
git add bot.py logic.py storage.py tests SCHEDULER_V2_UPGRADE.md
git commit -m "Batch availability, earliest qualified date, vote carry-over, rename test command"
git push
fly deploy -a dnd-with-scheduler
```

On startup, guild slash commands sync to the server via `GUILD_ID`; use `/test_scheduler` rather than `/once_scheduler` after deployment. Do not run `/test_scheduler` on an active campaign you want to preserve: like the old command, it replaces the active schedule.

## Important constraints

The `/start_scheduler`, `/reset_scheduler`, and `/test_scheduler` operations deliberately replace the active scheduler and wipe old votes; carry-over occurs only between naturally recurring polls. Carry-over requires the same actual date and time to remain on the new poll. A 4-week original poll is more likely to have overlapping future slots than a 1-week poll. A currently running poll is unaffected until the next naturally generated poll.

The older README may still mention `/once_scheduler` and the default limit of 12. This upgrade guide supersedes those entries until you update the README.
