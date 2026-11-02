# Remove duplicate slash commands (global + old server-specific copies)

## Why Discord shows duplicates

An earlier version registered `/start_scheduler`, `/end_scheduler`, `/reset_scheduler`, and `/scheduler_status` directly inside the old development guild through `copy_global_to()` and `sync(guild=...)`. The newer multi-server version registers commands **globally**. Discord keeps both kinds of registration until the older **guild-specific** registrations are explicitly removed, so commands can appear twice in that server.

This patch performs an **automatic one-time migration per server**, on bot startup after guilds are available. It clears the obsolete guild-specific command registrations without deleting globally registered commands. It also checks servers when the bot joins them. It records completed migrations in a small SQLite table so restarts and Discord reconnects don't repeat registration writes. If Discord's API rejects a cleanup request, the bot logs the error and retries on a future ready event. If new intentional guild-scoped commands are defined in a future version, the cleanup skips that guild to avoid deleting them.

**It does not delete schedule data, forum posts, RSVP records, preferences, or global commands.** The SQLite table is created automatically and uses the existing Fly persistent volume.

## Installation in PowerShell

Save `dnd-remove-duplicate-slash-commands.patch` in your Downloads folder.

```powershell
cd C:\Coding\dnd-with-scheduler

git apply --check "$HOME\Downloads\dnd-remove-duplicate-slash-commands.patch"
git apply "$HOME\Downloads\dnd-remove-duplicate-slash-commands.patch"

python -m unittest discover -s tests -v
python -m compileall -q bot.py storage.py guild_preferences.py

git add bot.py storage.py tests/test_slash_command_cleanup.py SLASH_COMMAND_CLEANUP.md
git commit -m "Remove obsolete guild-specific slash command registrations"
git push
fly deploy -a dnd-with-scheduler
```

Once the Fly machine starts, it performs the guild cleanup and logs `Cleared obsolete guild-specific slash commands ...; globals retained`. Reload the Discord client (desktop `Ctrl+R`) if the old names remain in Discord's local autocomplete cache. Global commands might need a little time to refresh.

If `git apply --check` fails, don't force the patch; share the error so it can be adjusted to your checkout. No new secrets or Fly environment changes are needed.
