# 🎲 D&D with Scheduler

A Discord bot that continuously schedules D&D sessions using DM-prioritized availability polls, capped bot-managed RSVPs, native Discord Scheduled Events, forum posts, reminders, and calendar exports.

Designed for **Python 3.12 + discord.py 2.7.1**, a **single Railway worker**, and **SQLite on a persistent Railway volume**.

> **Status:** The scheduling logic and SQLite RSVP tests pass locally. The bot integration has not been tested against a live Discord server. Review permissions, run a full test cycle in a test server, and check Railway logs before relying on it.

## How it works

1. An Admin or member with Manage Server permission runs `/start_scheduler` in Discord. It posts in `#scheduling-&-voting` (or in the command channel if that name does not exist).
2. The bot pings a role or `@everyone`, displaying an interactive **date X-axis / time Y-axis voting matrix**. Discord permits only five component rows, so each page displays at most 4 dates × 4 times. **Dates ▶ / Times ▶** open a *private* next page for that voter; they do not change the public page. Each user may vote for multiple date/time cells; clicking the same cell again cancels that vote.
3. After 3–7 days, the poll closes. If the DM voted, only slots the DM selected are eligible; the most-voted of those wins. Otherwise, the most-voted overall slot wins. Ties are broken randomly. If no eligible votes remain, the bot starts a new poll automatically.
4. The bot creates a native Discord Scheduled Event associated with your voice channel named `VC` (or an external event if that channel doesn't exist), posts an announcement with RSVP buttons, and creates a new thread in the `dnd-session-events` forum.
5. Players who voted for the **winning date/time cell** are auto-confirmed in first-vote order up to the attendee limit. Remaining voters for that cell are waitlisted. Other players may RSVP through the bot's button; cancellations automatically promote the oldest waitlisted member.
6. The event announcement has `RSVP / Cancel`, `Download iCal`, `Add to Google Calendar`, and `Discord event (Interested)` buttons. The bot sends the configured reminder *before the event starts*.
7. When the event ends, the bot starts a new poll after the specified post-event delay, unless `/once_scheduler` was used.

### Discord's native RSVP limitation

**Discord does not expose a bot API to subscribe someone to a Guild Scheduled Event as "Interested".** Auto-RSVP and attendee-limit enforcement are therefore **inside the bot**, not native Discord. People can separately click **Interested** on Discord's event. The bot does not count or cap native Interested participants, which might differ from bot RSVP totals.

The event announcement is an interactive bot RSVP post; the native Scheduled Event itself cannot be made into a custom bot-controlled RSVP poll. This design provides both surfaces without claiming they are synchronized.

### Decisions about ambiguous requirements

- **A vote is for a date + time cell**, not merely a weekday. Winning slots are selected using DM priority as described above.
- **Only members who voted for the winning slot** are automatically RSVP'd; voting for a losing slot does not mean you committed to a time you cannot attend.
- `reminder-before-event` means hours **before the upcoming event starts**. Your specification's phrase about creating a new poll before the previous event ends appears to be a copy/paste error. `poll-after-event` controls the next poll.
- Day windows start **seven days from the local date the poll opens**, then include the next `weeks × 7` days. Each poll is recalculated from its own start time.
- Bot event cancellations are limited to tracked events created by this bot in the server. The bot never removes unrelated members' events. `/reset_scheduler`, `/end_scheduler`, and `/once_scheduler` remove tracked poll/event announcement posts, delete tracked Scheduled Events that have not finished, and archive/lock old session threads, preserving discussion history instead of deleting it.
- **One scheduler per Discord server** and **one Railway bot replica**. Multiple instances connected to the same token/SQLite file are not supported.
- Timezone is configured with the service-wide `TIMEZONE` variable (default `America/Chicago`).

## Discord server structure

```text
D&D with Scheduler
├─ ⚠️scheduling-&-voting⚠️  (Text channel: set the *actual* Discord name in POLL_CHANNEL_NAME)
├─ Read This
│  ├─ signup-instructions
│  ├─ homebrew-rules-(do_later)
│  ├─ character-creation-(do_later)
│  └─ roles
├─ Chatting
│  ├─ general-yapping
│  ├─ dnd-session-events    (FORUM channel, not text)
│  └─ characters-&-lore-wiki (Forum channel)
└─ Voice Chat
   ├─ voice-chat
   └─ VC                    (VOICE channel)
```

Discord channel category labels (for example `Chatting`) do not affect the bot. Only **actual channel names** matter, and you can override them with environment variables. The `Admin` role name must match exactly, or members may use Manage Server permission. You do **not** need to enable privileged Message Content or Members intents just for slash commands.

## Commands

All commands require the `Admin` role or Discord's **Manage Server** permission.

| Slash command | Description |
| --- | --- |
| `/start_scheduler` | Clears and replaces the guild's current schedule, using new arguments. Continues in a loop. |
| `/reset_scheduler` | Clears old bot polls/events and starts a new **recurring** poll with the last saved settings, or defaults if none. |
| `/end_scheduler` | Stops the loop and clears tracked bot polls/event posts and scheduled events. |
| `/once_scheduler` | Clears old tracked items and schedules **one** event using saved settings, or defaults if none. |

`/start_scheduler` options (all optional):

| Option | Type | Default | Limits / Meaning |
| --- | --- | --- | --- |
| `title` | String | `Title` | 1–100 characters |
| `description` | String | `Description for event` | 1–1000 characters |
| `dm` | Discord member | Command creator | DM-prioritized slot votes |
| `weeks` | Integer | `2` | 1–4 |
| `days` | String | `thur,fri,sat,sun` | Weekday comma list (also supports `thu`) |
| `times` | String | `2pm,4pm,6pm` | Comma list; also accepts `4:15pm`, `14:00` |
| `poll-duration` | Integer | `3` | 3–7 days |
| `attendee-limit` | Integer | `12` | 1–100 confirmed guests |
| `poll-after-event` | Integer | `3` | 3–24 hours after event ends |
| `reminder-before-event` | Integer | `6` | 3–24 hours before event starts |
| `ping-role` | Role | `@everyone` | Must allow the bot to mention the target role, or be mentionable |
| `event-duration` | Integer | `4` | 1–24 hours |

Example:

```text
/start_scheduler title:"Curse of Strahd" description:"Session zero" weeks:2 days:"thur,fri,sat,sun" times:"2pm,4:15pm,6:30pm" poll-duration:3 attendee-limit:6 poll-after-event:3 reminder-before-event:6 event-duration:4
```

The exact syntax for slash command values is controlled by Discord's command UI; select role/member fields with Discord's picker.

## Create your Discord bot

1. Visit [Discord Developer Portal](https://discord.com/developers/applications), create an application called **D&D with Scheduler**, and open **Bot** to create/reset the bot token.
2. Under **OAuth2 → URL Generator**, select scopes `bot` and `applications.commands` (or use the Installation tab with Guild Install enabled).
3. Give the bot the necessary permissions: **View Channels, Send Messages, Embed Links, Attach Files, Read Message History, Manage Events, Create Public Threads/Send Messages in Threads, Manage Threads**. **Mention @everyone, @here, and All Roles** is needed to reliably ping `@everyone` or non-mentionable roles. The ability to create posts in the forum channel must also be allowed.
4. Invite the bot to your server. The `Admin` role is checked on members running commands, not required for the bot itself.
5. Create the text channel, **Forum** channel and VC voice channel matching your configured names. Run commands in your server, not in direct messages.

**Never commit the bot token to GitHub.** Set it as `DISCORD_TOKEN` in Railway's Variables settings.

## Run locally

Requirements: Python 3.12 and pip.

```bash
python -m venv .venv
# On Linux/macOS:
source .venv/bin/activate
# On Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env`: set `DISCORD_TOKEN`, optionally `DISCORD_GUILD_ID` for fast command registration in one test server, `TIMEZONE`, and **change `DB_PATH=./scheduler.sqlite3` locally** if you do not have `/data`.

```bash
python bot.py
```

Commands will sync to `DISCORD_GUILD_ID` when set, otherwise globally. Global slash commands may take time to propagate.

## Deploy to Railway using GitHub

1. Download/unzip this project and **create a GitHub repository** (for example `dnd-with-scheduler`). Push all files **except `.env`**, which is already gitignored.
2. Open [Railway](https://railway.com), create a **New Project → Deploy from GitHub repo**, and select this repository. Railway will build the included `Dockerfile`.
3. In your Railway service, add a **Volume mounted at `/data`**. SQLite is stored in `/data/scheduler.sqlite3` and survives redeploys. Without a volume, a new deployment can erase polls/RSVPs.
4. In Railway **Variables**, set:

   ```text
   DISCORD_TOKEN=your_private_token
   DB_PATH=/data/scheduler.sqlite3
   TIMEZONE=America/Chicago
   DISCORD_GUILD_ID=your_testing_server_id   # optional
   POLL_CHANNEL_NAME=scheduling-&-voting
   EVENTS_FORUM_NAME=dnd-session-events
   VOICE_CHANNEL_NAME=VC
   ```

5. Deploy. This is a **persistent background worker** using the Discord Gateway, not a public web service. You do not need a domain, public port, healthcheck HTTP endpoint, or Railway Cron Job.
6. Leave **replicas at 1**. In your test server, run `/once_scheduler` and vote. For a faster test, temporarily edit time constraints in a development branch or test the pure scheduling functions locally; production `/start_scheduler` enforces the 3–7 day poll duration.
7. Check the **Railway deployment logs** for permission errors. Pushes to GitHub can trigger automatic Railway redeploys.

More: [Railway services](https://docs.railway.com/services) · [Railway volumes](https://docs.railway.com/volumes) · [Discord Scheduled Events](https://support.discord.com/hc/en-us/articles/4409494125719-Scheduled-Events)

## Persistence and production notes

- Votes, sessions, RSVP positions, and config persist in SQLite. Persistent Discord components are re-registered on startup. The scheduler checks due work approximately every 30 seconds, retries failures, and does not require a cron service.
- The backend is intended to run only once. Don't scale it to several replicas; the database does not provide distributed locking.
- The bot records event IDs and only manages events it created. Reset/stop archives session threads instead of deleting player discussion.
- Partial Discord API failures are logged; the bot attempts basic recovery. A crash during the narrow window between Discord creating an event and saving its ID can still produce a duplicate; administrators should inspect the Events tab after a crash during event creation.
- Calendar `.ics` files are generated on demand; Google Calendar buttons open a *pre-filled event*, which the user must save. Calendar exports don't synchronize future RSVP changes.
- Native Discord Scheduled Events are separately visible to the guild; their **Interested** button is independent of the bot's cap and waitlist.
- SQLite volume is persistent storage, not a backup. Back up the volume periodically.
- Voice-channel native events are started and completed by the bot when due; if the bot is unavailable at start, those transitions are attempted on recovery.

## Tests

```bash
python -m unittest discover -s tests -v
```

Tests cover day/time parsing, voting priority, week windows, RSVP/waitlist promotions and calendar exports. They do not simulate a live Discord API.

## Files

- `bot.py` — Discord bot, slash commands, buttons, reminder/recurrence worker.
- `scheduler_logic.py` — deterministic parsing, scheduling, voting, calendars.
- `storage.py` — SQLite state and RSVP records.
- `tests/test_scheduler.py` — unit tests for scheduling and data rules.
- `Dockerfile` and `railway.json` — Railway build/deployment instructions.
- `.env.example` — configuration template.
