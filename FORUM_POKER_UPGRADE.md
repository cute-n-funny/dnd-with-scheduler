# Independent `/poke_forums` thread keeper

A per-server, opt-in keepalive for individual Discord **forum threads**, regardless of the forum selected for D&D session posts in `/set_preferences`.

## How it works

- Administrators use `/poke_forums add:123456789012345678` or `add:123456789012345678,234567890123456789` to add individual thread **IDs**. Invalid IDs do not prevent other valid entries from being saved.
- `/poke_forums remove:123456789012345678` stops monitoring that thread (without deleting old messages).
- `/poke_forums ping-now:true` immediately refreshes **all monitored threads in the current server**, including any just added.
- `/poke_forums` with no arguments does not change anything and shows the current monitored-thread count.
- Without `ping-now:true`, a newly added thread's first poke is scheduled **three days after adding**. Later pokes are three days after each successful poke. Tasks survive restarts using SQLite's `forum_pokes` table, separate from schedule and preference records.
- Each thread gets a bot-authored post containing `Poked on: **MM/DD/YYYY**` (date is **UTC**). Future pokes edit this **same message**, not add repeated messages. The database stores the original message ID. If the saved message is deleted, the bot checks the last 250 posts for an earlier bot-authored marker before sending a replacement.
- **Important Discord behavior:** Editing a message is *not* thread activity. When already active, the bot briefly changes the thread's auto-archive duration and restores the original value. If archived, it unarchives the thread. Discord documents changing auto-archive duration and unarchiving as activity. This avoids repetitive new messages. It does *not* guarantee pinned placement or override how Discord sorts every forum view.
- Errors are persisted; failed threads retry **one hour** later. To avoid noise, a repeated identical failure is not reposted to the error channel. If one or more valid channels are configured in `/set_preferences channel-for-commands`, the bot posts the first error to the **first existing text channel** from that list. Otherwise the error is logged. Errors from manual `ping-now` also appear in the command's private response.
- `/poke_forums` honors the optional command-channel whitelist and admin gate, but **does not require** announcement or forum preferences. Existing schedules, Session Details, forum posts, or role pings are not modified by this feature.

## Discord permissions

Give the bot **View Channel**, **Read Message History**, and **Send Messages in Threads**, plus **Manage Threads** in the relevant forum. Manage Threads is important because the bot needs to refresh auto-archive activity for threads it did not create. The bot cannot bypass locks, other permission failures, inaccessible threads, or Discord limits. These will be reported/retried.

## Install (PowerShell)

Download **`dnd-forum-poker-final.patch`** (not the earlier `dnd-forum-poker.patch` or `dnd-forum-poker-fixed.patch`) to Downloads, then from your repository:

```powershell
cd C:\Coding\dnd-with-scheduler
git status --short
git apply --check "$HOME\Downloads\dnd-forum-poker-final.patch"
git apply "$HOME\Downloads\dnd-forum-poker-final.patch"
python -m unittest discover -s tests -v
python -m compileall -q bot.py forum_poker.py storage.py guild_preferences.py

git add bot.py forum_poker.py Dockerfile tests/test_forum_poker.py FORUM_POKER_UPGRADE.md
git commit -m "Add persistent forum thread keepalive command"
git push
fly deploy -a dnd-with-scheduler
```

The SQLite table is created automatically on the existing persistent Fly volume. No new environment variables are needed. If `git apply --check` fails, stop and share the message rather than forcing the patch. Test the command on a non-critical forum thread first.
