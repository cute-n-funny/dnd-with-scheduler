# D&D Scheduler — Discord UX Upgrade

This patch is based on the working version **after** the private `#scheduler` / public `#⚠️scheduling-&-voting⚠️`, one-minute `/once_scheduler`, and Windows unit-test patches. It **does not change** `fly.toml`, the Fly app name, channel IDs, permissions, `GUILD_ID`, or your regular schedule-selection rules.

## What players will see

### Availability poll (`#⚠️scheduling-&-voting⚠️`)

- **Vertical, phone-friendly list**: each candidate date has its own section with readable time-by-time counts and distinct voter count for that day. The deadline is shown in each player's Discord-local format, with a relative countdown. Candidate time labels are in your configured **server timezone**.
- **Set My Availability** opens a private menu. Players select a date and use the **multi-select time dropdown** to choose all times they can attend for that date, then repeat on other dates. The initial selections reflect their saved votes.
- **Select All Times** and **Clear This Day** work on the currently selected date.
- **Save Availability** saves the complete form in **one atomic database transaction**. A success message summarizes slots and dates; otherwise the form warns about **unsaved changes**. Saved unchanged votes keep their original timestamps, preserving first-come RSVP priority.
- **Copy Previous Preferences** optionally selects matching *weekday and time* patterns from the immediately preceding poll for **all** matching upcoming dates. It modifies only the current form, **not saved votes**. Players must review the dates and press **Save Availability**. This feature will have no past poll data on the first run or after a reset that deleted old votes.
- On poll close, the selected day/time and how many people voted for them are displayed. The scheduling algorithm remains the original **unique people per day, then time within that day; DM-voted days restricted if the DM voted**.

### Session announcement and RSVP

- A clearer **Session Details** embed shows the start time in each user's Discord-local timezone, seats, waitlist count, DM, forum link, and event link.
- **Manage My RSVP** opens a private view showing **Confirmed**, **Waitlisted (#position)**, or **Not RSVPed**, with an explicit **Confirm Attendance**, **Join Waitlist**, or **Cancel My RSVP** button.
- The waitlist still promotes the earliest waiting member if a confirmed member leaves. The native Discord Event's **Interested** counter is independent of the bot-managed RSVPs.
- If a second panel or participant changes your status while you have the panel open, a stale action asks you to review again rather than unexpectedly toggling the wrong state.

### Private admin controls (`#scheduler`)

- **`/scheduler_status`** (Admin/Manage Server or `Admin` role, **only** in private `#scheduler`) provides an ephemeral status card with phase, cycle, test/recurring mode, voter and RSVP counts, deadlines, next action, and links to the public poll/event/RSVP where applicable.
- Existing `/start_scheduler`, `/reset_scheduler`, `/end_scheduler`, and `/once_scheduler` behaviors remain unchanged; the one-minute test is still silent (no @everyone ping or reminder) and never cycles.

## How to apply (PowerShell)

Download `dnd-scheduler-discord-ux-upgrade.patch` to Downloads. In your local repository:

```powershell
cd C:\Coding\dnd-with-scheduler

git status --short
git apply --check "$HOME\Downloads\dnd-scheduler-discord-ux-upgrade.patch"
git apply "$HOME\Downloads\dnd-scheduler-discord-ux-upgrade.patch"

python -m unittest discover -s tests -v
python -m compileall -q bot.py logic.py storage.py

git add bot.py storage.py tests/test_ux.py UX_UPGRADE.md
git commit -m "Improve Discord voting, RSVPs, and admin status UX"
git push
fly deploy -a dnd-with-scheduler
fly logs -a dnd-with-scheduler
```

**Do not reapply the earlier patches if they're already installed.** The UX patch expects them to be present. If `git apply --check` fails, don't use `--force` or reset your code; show the error for a targeted patch adjustment.

For a quick smoke test, run `/once_scheduler` in private `#scheduler`, vote in the public channel, save multiple slots, then inspect the event RSVP panel. Test `/scheduler_status` before and after the test poll closes. **Warning:** `/once_scheduler` replaces a currently active recurring schedule; use `/reset_scheduler` or `/start_scheduler` to resume your campaign.

## Operational notes

- **No new secrets or Fly configuration are required.** The existing SQLite file is used as-is; no schema migration is needed for this UX upgrade.
- Existing public poll buttons remain compatible across bot restarts (their `custom_id` is unchanged). Users with an old open ephemeral form should reopen it after deployment.
- Discord's native controls limit a dropdown to 25 options. The existing scheduling limits (up to 4 weeks × 4 weekdays = 16 dates; up to 12 times per date) fit within that limit. The form is private and its interactive session lasts up to 10 minutes.
- The public embed uses stacked fields (not a browser calendar) and is constrained by Discord embed size limits.
- The included tests cover core UX logic without requiring a Discord token. **Live Discord/Fly integration has not been performed as part of this patch.**
