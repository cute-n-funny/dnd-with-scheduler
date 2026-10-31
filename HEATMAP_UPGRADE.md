# Live weekly Discord availability heatmap

This upgrade is for the current D&D Scheduler version that includes `/test_scheduler`, batch multi-date voting, and the private `#scheduler` / public voting channel split.

## What players see

In the public `#⚠️scheduling-&-voting⚠️` poll, the bot posts one PNG heatmap **per poll week** (1–4 weeks). Each picture has seven day columns and a row for each configured poll time, with a count of distinct players inside each square. Colors show 0 (gray), low (red), moderate (orange), almost full (yellow), and the `attendee_limit` met (green). Greyed-out cells with a dash are times not offered by the poll.

The heatmap updates **in place** whenever a player presses **Save Availability**. Users still enter votes with the existing **Set My Availability** multi-select menus; the colored squares themselves are not clickable. The existing Discord poll message and its buttons are reused, so the public voting channel isn't flooded with new messages. When a poll closes, the final heatmap remains with the winning slot outlined in white and the voting button removed. If rendering or uploading the images fails, the bot falls back to its existing text-based poll.

## Install (Windows PowerShell)

Download `dnd-scheduler-live-heatmap.patch` into your Downloads folder, then run:

```powershell
cd C:\Coding\dnd-with-scheduler

git apply --check "$HOME\Downloads\dnd-scheduler-live-heatmap.patch"
git apply "$HOME\Downloads\dnd-scheduler-live-heatmap.patch"

python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python -m compileall -q bot.py heatmap.py logic.py storage.py

git add bot.py heatmap.py requirements.txt tests/test_heatmap.py HEATMAP_UPGRADE.md .github/workflows/tests.yml
git commit -m "Add live weekly Discord availability heatmaps"
git push
fly deploy -a dnd-with-scheduler
```

**Don't reapply earlier patches if they're already installed.** This patch expects the existing multi-date voting upgrade on GitHub. If `git apply --check` fails, share the error; don't force it.

## Test

In private `#scheduler`, run `/test_scheduler` (note: this still replaces your current active schedule). In the public channel, use **Set My Availability** and save a few combinations. Check that the weekly image changes without sending a new public message. After the one-minute test poll closes, verify the final image and native event. For production schedules, 1–4 distinct week images are created automatically.

## Technical notes

- Uses `Pillow` in `heatmap.py` to generate PNG images in memory, with no file cache or SQLite schema changes.
- Uses `discord.File` and `attachment://...` embed URLs. Discord allows 10 embeds and the scheduler uses at most 5 (poll summary plus 4 images).
- The poll's existing vote button/custom ID is unchanged, so it can be restored after bot restarts. Images are regenerated from saved votes on the next successful vote or update.
- No additional hosting service, webhook, Fly secret, or frontend is needed. The Dockerfile installs `requirements.txt` during deployment, including Pillow.
- Google/iCal export, DM voting priority, earliest-qualifying-slot rule, automatic carry-over, RSVPs, and private bot status notices are unchanged.
- The heatmap uses configured poll times, **not every hour of the day**. Times are labeled in the scheduler's configured timezone; Discord date timestamps in the text summary render in a viewer's local timezone.
- Tests can run without a Discord token but **live Discord and Fly.io integration still need to be verified after deployment**.
