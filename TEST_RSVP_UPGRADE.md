# D&D Scheduler — Test and normal automatic RSVPs

This patch is for the current code after the availability, minimum-votes, forum-history, Player-role and location upgrades.

The **one-minute `/test_scheduler` event** now uses saved availability windows to identify the winning *voted poll time* and pre-fill the RSVP list even when the player selected fewer hours than the normal campaign's `event_duration`. A default three-hour availability choice, for example, qualifies for the one-minute test. Test polls already open before upgrading are supported: the bot reconstructs ballots from the saved availability-window records before closing the test poll. Every person counts once per exact voted time.

The bot still picks the winning time among the **poll's future date/time options** for test voting; the real simulated Discord event continues to start **one minute after test poll closure**, last one minute, and never create a forum post or send mass role pings. Test mode continues to bypass `min_votes`, including the existing zero-vote smoke test.

For both normal and test events, the qualifying voters are automatically entered in RSVP order: **DM first if they qualify for the chosen time**, then others in original first-vote order; up to `attendee_limit` become confirmed, with extras waitlisted. The Session Details message includes the existing announcement **"The following users have been added after voting:"** followed by the initially confirmed voters. A tie-safe SQLite ordering fix preserves the DM-first ordering even if auto-RSVP timestamps are identical. Clearing a schedule now also clears its saved availability windows, preventing an unrelated new test or campaign poll from accidentally inheriting orphaned RSVP preferences. Historical forum posts are not touched.

**Normal sessions are unchanged**: availability must still cover the full configured session duration, the minimum distinct-date-voter rule remains in effect, and recurring retries and calendar/heatmap behavior stay intact.

## Installation (Windows PowerShell)

Download `dnd-test-and-normal-auto-rsvps.patch` into Downloads and then run:

```powershell
cd C:\Coding\dnd-with-scheduler
git status --short
git apply --check "$HOME\Downloads\dnd-test-and-normal-auto-rsvps.patch"
git apply "$HOME\Downloads\dnd-test-and-normal-auto-rsvps.patch"
python -m unittest discover -s tests -v
python -m compileall -q bot.py availability.py availability_ui.py storage.py logic.py

git add bot.py availability.py availability_ui.py storage.py tests/test_once_scheduler.py tests/test_min_votes.py tests/test_test_mode_rsvps.py TEST_RSVP_UPGRADE.md
git commit -m "Auto-RSVP matching availability in test and normal sessions"
git push
fly deploy -a dnd-with-scheduler
```

If `git apply --check` fails, do not force it. Share the error and we can adjust the patch to your checkout. `/test_scheduler` continues to replace your active schedule; testing it during a live campaign can clear active poll/RSVP messages, though historic forum posts are preserved.

No database schema changes, external services or new secrets are necessary. The full test suite and live Discord integration still need to be verified on your Windows checkout and Fly deployment.
