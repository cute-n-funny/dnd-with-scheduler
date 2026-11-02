# Keep forum history and simplify test sessions

This update targets the bot **after** the minimum-votes/retry upgrade and the existing availability heatmap / Players RSVP updates.

## Behavior changes

- **Default times**: When `/start_scheduler` is called without `times`, the scheduler now offers 2:00, 2:30, 3:00, 3:30, 4:00, 4:30, 5:00, 5:30, 6:00, 6:30 and 7:00 PM (11 options). Explicitly supplied `times` values are unchanged. Existing schedules retain their previously saved times until restarted with the new defaults.
- **Forum history**: `/end_scheduler`, `/reset_scheduler`, `/start_scheduler` replacing an active schedule, and `/test_scheduler` no longer delete existing session forum posts. Forum posts remain accessible after the scheduler's active SQLite record is cleared. This does **not** change cleanup of the bot's old voting/RSVP messages or future scheduled Discord Events.
- **Test sessions**: `/test_scheduler` still runs a one-minute poll and one-minute scheduled event with RSVP details, but it does not create a forum thread. It no longer requires a forum channel to be configured and uses the external Discord event location `Discord — test session` when no voice channel is available.
- **Real sessions**: Successful normal polls continue creating their session forum posts and are still required to have a forum channel configured.

## Install from PowerShell

Save the patch `dnd-scheduler-preserve-forums.patch` in Downloads and then run:

```powershell
cd C:\Coding\dnd-with-scheduler
git apply --check "$HOME\Downloads\dnd-scheduler-preserve-forums.patch"
git apply "$HOME\Downloads\dnd-scheduler-preserve-forums.patch"
python -m unittest discover -s tests -v
python -m compileall -q bot.py logic.py storage.py availability.py availability_ui.py heatmap.py
git add bot.py logic.py tests/test_forum_history.py FORUM_HISTORY_UPGRADE.md
git commit -m "Preserve forum history and skip test forum posts"
git push
fly deploy -a dnd-with-scheduler
```

The test command still replaces the active schedule. It does **not** delete historical forum threads, but it does remove active bot-owned poll/RSVP messages and future bot-managed scheduled events as before. It is safest to test outside a live campaign.

No SQLite schema change or new Fly.io setting is needed. Forum preservation is prospective: threads deleted by older versions cannot be restored by this patch.
