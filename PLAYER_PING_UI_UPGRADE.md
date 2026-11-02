# Player ping and availability-control labels/colors

This patch is intended for the current D&D Scheduler after the previous minimum-vote, forum-history, and availability upgrades.

## Changes

- The normal `/start_scheduler` public poll now resolves the server role named **Player** (singular) or **Players** (plural), ignoring case, and mentions it by Discord role ID. If both roles exist, **Player** takes priority. A custom explicitly supplied `ping_role` continues to override the default for polls and reminders. If neither role is found, no broad `@everyone` ping is sent.
- Both the main poll message and its heatmap-upload fallback explicitly permit role mentions, while **blocking `@everyone` and user mentions**. `/test_scheduler` remains silent for role pings.
- Rename the two dropdown placeholders to **Choose your earliest start time** and **Choose how many hours you can stay**.
- **Add Times** is now **green** and **Remove Times** is **red**; Close stays gray. Autosave and the existing voting logic are unchanged.

**Important:** Discord can only deliver a role notification when the role is set to **Allow anyone to @mention this role** or the bot has **Mention @everyone, @here, and All Roles** permission in the relevant channel. Users may suppress push notifications in their Discord preferences. The bot logs a warning for an unmentionable default role when it lacks guild-wide mention permission.

## Apply (PowerShell)

Download `dnd-player-ping-ui.patch` to Downloads, then:

```powershell
cd C:\Coding\dnd-with-scheduler

git apply --check "$HOME\Downloads\dnd-player-ping-ui.patch"
git apply "$HOME\Downloads\dnd-player-ping-ui.patch"

python -m unittest discover -s tests -v
python -m compileall -q bot.py availability_ui.py logic.py storage.py

git add bot.py availability_ui.py tests/test_instant_availability_ui.py tests/test_player_ping_ui.py PLAYER_PING_UI_UPGRADE.md
git commit -m "Fix Player role pings and update availability controls"
git push
fly deploy -a dnd-with-scheduler
```

**Do not reapply prior patches.** If `git apply --check` fails, don't force it—share the error for a version-matched correction. No new database tables or secrets are required.

After deploying, start a **normal** poll to verify the ping (a test poll deliberately will not ping). `/start_scheduler` replaces the current active schedule, so plan this test around your campaign schedule. Existing poll posts cannot be made to send retroactive notifications.
