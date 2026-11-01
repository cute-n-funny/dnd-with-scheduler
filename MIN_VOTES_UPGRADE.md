# D&D Scheduler — Minimum votes and delayed retry

This patch targets the current bot after the latest availability, heatmap and Players/DM RSVP upgrades.

**New settings for `/start_scheduler`:**

| Slash parameter | Default | Allowed | Meaning |
| --- | --- | --- | --- |
| `min_votes` | `3` | `3–12` | Minimum **distinct voters on the calendar DATE** required to schedule a session. Selecting several times on that date counts as **one** person for this minimum. |
| `poll_duration` | `2` days | `2–6` days | How long a normal public poll stays open. |
| `attendee_limit` | `8` | Existing `1–99` | Maximum automatic confirmed RSVPs. The earlier rule choosing the earliest exact date/time with `attendee_limit` distinct voters remains unchanged. |

**If no date is eligible:** The bot closes the poll without creating a Discord Scheduled Event or forum post and saves a retry deadline of `12 × poll_duration` hours after the poll closes (24 hours when using the default of 2 days). A persistent `waiting_retry` state resumes correctly after Fly machine restarts. The worker creates the next poll at or after the deadline. Any still-matching future availability ballots carry over using the existing recurring-poll mechanism.

**If at least one date is eligible:** The existing DM-voted-day restriction applies, then the bot chooses the earliest exact slot that meets `attendee_limit`; if none does, it uses the most popular eligible day and time with random ties, as before. A date can have enough distinct voters to satisfy `min_votes` while each exact time has fewer than that many voters; its chosen time will have only those voters auto-RSVPed.

**`/test_scheduler` bypass:** The one-minute test allows one vote to win even if `min_votes` is 12. Existing zero-vote smoke-test behavior (creating the test event anyway) is preserved. Test mode never queues a delayed retry or starts another poll.

**Existing schedules:** A saved scheduler created before this patch keeps its stored `poll_duration` (e.g. 3 days) until replaced. The missing `min_votes` key is interpreted as 3. A prior saved value of 7 days is outside the new range if you reset the schedule; start a fresh schedule with 2–6 days. The test command normalizes a legacy seven-day configuration for its temporary one-minute run.

## Installation (PowerShell)

Download `dnd-scheduler-min-votes-retry.patch` to your Downloads folder and run:

```powershell
cd C:\Coding\dnd-with-scheduler

git apply --check "$HOME\Downloads\dnd-scheduler-min-votes-retry.patch"
git apply "$HOME\Downloads\dnd-scheduler-min-votes-retry.patch"

python -m unittest discover -s tests -v
python -m compileall -q bot.py logic.py storage.py availability.py availability_ui.py heatmap.py

git add bot.py logic.py tests/test_once_scheduler.py tests/test_min_votes.py MIN_VOTES_UPGRADE.md
git commit -m "Add minimum-vote threshold and delayed poll retry"
git push
fly deploy -a dnd-with-scheduler
```

If `git apply --check` fails, don't force it or reset your repo. Share the error for a patch adjusted to your current checkout. No SQLite schema changes or additional Fly secrets are necessary.

For live testing, `/test_scheduler` replaces your active schedule, and a real two-day poll is needed to observe the normal 24-hour retry. The private `/scheduler_status` will display the saved retry deadline.
