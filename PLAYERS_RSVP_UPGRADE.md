# Players role ping, default 3 hours, and DM-first RSVP upgrade

This patch targets the bot **after** the Heatmap V2 and **Instant Availability UX** patches, including `/test_scheduler` and automatic voting-window persistence.

## Behavior

- **Poll announcement:** A normal poll mentions the Discord **Players** role (case-insensitive lookup of the actual role ID), rather than pinging `@everyone`. The existing explicit `ping_role` option remains a custom override for polls and scheduled reminders. If `Players` is missing, the bot sends the message **without a role ping** and logs a warning; it never falls back to `@everyone`.
- **Start hours:** The Available Time: Hours select defaults to **3 hours** whenever a player opens the panel. Its available choices remain 1 through the scheduler's `hours` limit (3–8). Previously saved windows remain unchanged.
- **Auto-RSVP:** When the poll closes, only players whose availability qualifies for the **exact winning session start and full event duration** are eligible for automatic RSVP. The **DM is first if and only if they qualify**, followed by others in timestamp order of their first qualifying vote. The first `attendee_limit` become confirmed; additional qualifying players join the waitlist. No duplicate user entries.
- **Session Details:** New non-test event announcements ping **Players** (regardless of the custom poll/reminder role) and remind players to use **Download iCal (.ics)** or **Add to Google Calendar**. The same message lists the players initially auto-confirmed from votes. Additional qualifying players appear in the RSVP waitlist in the embed. For unusually large confirmed groups the announcement truncates the inline mention list to fit Discord's 2,000-character message limit; the RSVP embed still shows confirmed/waitlist counts.
- **Test mode:** `/test_scheduler` announces neither the Players role nor any custom role on poll or event. Individual user mentions in the announcement are displayed but deliberately do **not** generate user pings. Test timing and event behavior are unchanged.
- **Existing event posts:** This changes **newly created** Session Details messages, not old posts. No database migration is required.

For the role mention to notify users, your server needs a role named **Players**, and Discord must allow the bot to mention it (make the role mentionable or grant the bot permission to mention roles).

## Install in PowerShell

Download `dnd-scheduler-players-dm-rsvp.patch` into Downloads. In the local repo:

```powershell
cd C:\Coding\dnd-with-scheduler

git status --short
git apply --check "$HOME\Downloads\dnd-scheduler-players-dm-rsvp.patch"
git apply "$HOME\Downloads\dnd-scheduler-players-dm-rsvp.patch"

python -m unittest discover -s tests -v
python -m compileall -q bot.py logic.py availability_ui.py availability.py heatmap.py storage.py

git add bot.py logic.py availability_ui.py tests/test_instant_availability_ui.py tests/test_players_rsvp_announcements.py PLAYERS_RSVP_UPGRADE.md
git commit -m "Ping Players and auto-confirm DM first for sessions"
git push
fly deploy -a dnd-with-scheduler
```

If `git apply --check` fails, **don't** force the patch. Review `git status --short` and show the error for a targeted patch adjustment.

For a smoke test, use `/test_scheduler` to confirm no role ping. To verify the Players role mention and actual DM-first automatic RSVPs, create a normal poll in a safe testing window. `/test_scheduler` replaces the active recurring schedule; plan your test accordingly.
