# D&D Scheduler — multi-server preferences

This patch replaces channel-name and global environment-variable fallbacks with **persistent SQLite preferences per Discord server**. It preserves existing campaign scheduling, voting, forum-history, calendar exports and test mode.

## `/set_preferences` options

The five option names shown in Discord use hyphens:

| Option | Default | Behavior |
|---|---|---|
| `channel-for-announcements` | unset | **Required configuration**. One text channel for polls, RSVPs and reminders. Selecting another replaces it. |
| `channel-for-forum` | unset | **Required configuration**. One forum channel for real session threads. Selecting another replaces it. |
| `channel-for-commands` | none | Toggles one allowed text channel. Repeating the same selection removes it. Several channels may be allowed; none (or all deleted) means commands work from any text channel. |
| `activate-discord-events` | `false` | Controls creation of native Discord Scheduled Events. Bot RSVP posts, forum discussions and calendar download continue without them. |
| `pingable-role` | unset | One role, excluding the server's `@everyone`. Blank means **no role ping**. |

The required channels are **required in stored settings**, not required inputs on every `/set_preferences` call. This lets an admin update one field without resending all five. Invalid supplied fields are reported individually, while valid fields from the same command are saved. Removing/deleting a configured announcement or forum channel blocks scheduler commands until fixed. A deleted configured command channel is ignored; if no valid command channels remain, admin commands are available in any text channel.

**Role-mention permission**: for the pingable role to trigger notifications, it must be mentionable or the bot must have permission to mention roles. `/test_scheduler` remains silent even when a role is configured. If you previously relied on automatic `Player`/`Players` role discovery, configure that role explicitly with `/set_preferences`.

**Admin security**: only a server administrator or someone with Manage Server permission (or the bot's legacy Admin role) can run scheduler management commands and `/set_preferences`. Preference changes respect any existing command-channel whitelist, unless the channels were deleted. The bot no longer requires a channel named `scheduler`.

**Native Discord Events**: disabled by default in new preferences; when enabled, the bot creates and manages Discord native events. When disabled, bot-managed Session Details, RSVPs and forum posts still work. No Discord Event link is shown when no native event was created. Existing native events from earlier runs may still be cleaned up by reset/end.

## Installation

Download the `dnd-multi-server-preferences.patch` file and save it to Downloads. In PowerShell:

```powershell
cd C:\Coding\dnd-with-scheduler
git status --short
git apply --check "$HOME\Downloads\dnd-multi-server-preferences.patch"
git apply "$HOME\Downloads\dnd-multi-server-preferences.patch"
python -m unittest discover -s tests -v
python -m compileall -q bot.py guild_preferences.py storage.py logic.py availability_ui.py

git add bot.py guild_preferences.py Dockerfile tests/test_channel_routing.py tests/test_player_ping_ui.py tests/test_players_rsvp_announcements.py tests/test_guild_preferences.py SERVER_PREFERENCES_UPGRADE.md
git commit -m "Add persistent server preferences and channel routing"
git push
fly deploy -a dnd-with-scheduler
```

After deployment, use `/set_preferences` to set **both required channels** in each server before starting or resetting a schedule. **Existing schedule records are not migrated to preferences automatically.** This deliberate fail-closed behavior prevents a bot being invited to a new server from posting into an unintended channel. The new table is created automatically in the same persistent SQLite database on Fly; ensure that its volume survives restarts and that only one bot process uses it.

`/start_scheduler` keeps its legacy `ping_role` option for existing clients, but new announcements use **the guild's `pingable-role` preference** rather than the old per-command override. A new preference takes effect for future announcements without restarting or resetting the schedule. Existing published announcements are not reposted.

Global slash commands are now registered across servers regardless of `GUILD_ID` (which is ignored). Discord may take time to propagate them across all servers. You may remove the old `GUILD_ID` Fly setting when convenient. Existing `SCHEDULER_CHANNEL_ID`, `SCHEDULING_CHANNEL_ID` and `FORUM_CHANNEL_ID` deployment variables no longer control routing.
