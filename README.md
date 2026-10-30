# D&D with Scheduler

A persistent Discord bot for polling player availability, choosing a D&D session slot, creating a native Discord Scheduled Event plus forum post, managing limited RSVPs/waitlists, sending reminders, and repeating the cycle. Built with `discord.py 2.7.1`, Python 3.12, SQLite, and Fly.io.

**Deployment status:** Source code only. It is not automatically uploaded to your GitHub account or deployed to Fly.io. Follow the steps below with your own Discord bot token and Fly account.

## What it does

1. `/start_scheduler` posts an availability grid in the scheduling channel and pings a role.
2. Players click **Vote / Edit Availability**, then choose a date and time in a private interactive menu. Repeat to vote for more slots. Clicking again removes an existing vote. The public grid updates per-slot counts.
3. After `poll_duration` days, the bot counts **distinct voters per calendar day**; if the DM voted, only dates the DM selected are eligible. The top day wins (random tie). Within that day, the most popular time wins (random tie).
4. If nobody voted for a still-valid slot, a new availability poll is posted with the same configuration.
5. The bot creates a **native Discord Scheduled Event** (in `voice-chat`/`VC` if found; otherwise an external-location event), creates a post in the `dnd-session-events` **forum** channel, and posts an RSVP announcement in the scheduling channel, pinging the role again.
6. Users who voted for **the winning date-and-time slot** are auto-added to the bot's RSVP list in the order they first voted for that slot. The first `attendee_limit` are confirmed, the rest waitlisted. Anyone else can join the bot RSVP list using **Join / Leave RSVP**. A confirmed cancellation promotes the earliest waitlisted person.
7. The bot attempts to **start and complete the native Discord event automatically** at the scheduled times.
8. The RSVP message includes **Add to Google Calendar**, **Download iCal (.ics)**, and a link to the native Discord Scheduled Event. Users who want native Discord *Interested* notifications must click that themselves.
9. A reminder pings the role `reminder_before_event` hours before **session start**. After the session ends, a new poll opens `poll_after_event` hours later, unless the schedule was created with `/once_scheduler`.

## Discord setup

Create a bot application at the [Discord Developer Portal](https://discord.com/developers/applications). Under **Bot**, create/reset the token, then store it as a Fly secret; **never commit it to GitHub**.

Under OAuth2 / Installation, invite the bot with the `bot` and `applications.commands` scopes. Grant permissions:

- View Channels, Send Messages, Read Message History, Embed Links, Attach Files
- Create Events and Manage Events
- Send Messages in Threads; permission to post in the `dnd-session-events` Forum channel
- Mention @everyone, @here, and All Roles **if** using the default `@everyone` ping (or role ping permissions)

The bot uses default gateway intents, so it **does not require Message Content** or **Server Members** privileged intents.

Your server should have:

| Channel | Type | Purpose |
| --- | --- | --- |
| `scheduler` | **Private text** (only you + bot) | Slash commands, startup/shutdown notices |
| `⚠️scheduling-&-voting⚠️` (or any name containing `scheduling`) | **Public text** | Polls, RSVP announcements, reminders |
| `dnd-session-events` | **Forum** | One forum thread/post for each scheduled session |
| `voice-chat` or `VC` | Voice (optional) | Native Scheduled Event's channel |

If Discord names your channels differently, set `SCHEDULER_CHANNEL_ID` for private bot controls, `SCHEDULING_CHANNEL_ID` for public polls, and `FORUM_CHANNEL_ID` for the forum as environment variables or Fly secrets. To find an ID, enable Discord Developer Mode and **Copy Channel ID**.

Only members with Administrator, Manage Server, or a role literally named `Admin` are allowed to execute the four slash commands. Every guild has at most one active scheduler; running the start, reset, or once command replaces it.

## Private control channel and public voting channel

Your server uses **two different text channels**:

- **`#scheduler` is private** (visible only to the admin and the bot). All four slash commands (`/start_scheduler`, `/reset_scheduler`, `/end_scheduler`, `/once_scheduler`) **must be run here**. Startup/shutdown notices appear here only. Admin permissions are still required; running a slash command anywhere else is rejected with a private response.
- **`#⚠️scheduling-&-voting⚠️` is public** (viewable by players). Availability polls, voting interactions, RSVP messages, and reminders are posted here. The bot also creates forum posts in `dnd-session-events` and Discord Scheduled Events as before.

Use **separate** channel IDs (Discord Developer Mode → right click each channel → Copy Channel ID):

```bash
fly secrets set SCHEDULER_CHANNEL_ID=YOUR_PRIVATE_SCHEDULER_CHANNEL_ID SCHEDULING_CHANNEL_ID=YOUR_PUBLIC_VOTING_CHANNEL_ID -a dnd-with-scheduler
```

**Do not set `SCHEDULING_CHANNEL_ID` to the private `#scheduler` channel.** The bot refuses to route public polls there. If you leave the IDs unset, it looks for the exact name `scheduler` for commands and a public text channel whose name contains `scheduling` for polls. Providing both IDs is more reliable.

Configure Discord's own **channel permissions** so players cannot view `#scheduler` and the bot can send messages in both channels. A bot command restriction alone does not make a channel private. Discord may still show slash commands in autocomplete in other channels; execution outside `#scheduler` is rejected.

The updated `fly.toml` sets `kill_signal = "SIGTERM"` and `kill_timeout = 20` to give graceful shutdown notices more time.

- On a successful bot start, it posts **Fly Services Have Started** in private `#scheduler` once per process start.
- On a **graceful** shutdown (such as a deploy/restart), it attempts to post **Fly Services Have Shutdown** in private `#scheduler`.
- On an abrupt crash, the offline bot cannot send a message; on its **next startup**, it reports the previous shutdown retrospectively, then sends **Fly Services Have Started**.

**Important limitation:** An offline bot cannot post the instant it crashes or loses connectivity. To alert you while it remains offline, use an independent external monitor. These built-in messages are best-effort and the last known running/stopped state is saved in SQLite.

## Slash commands

Discord slash option names use underscores (`poll_duration`) rather than the hyphens shown in your initial spec (`poll-duration`).

| Command or option | Default | Behavior |
| --- | --- | --- |
| `/start_scheduler` | — | New recurring schedule; clears previous bot schedule and future bot-created native events |
| `title` | `Title` | Title of poll and session (max 100 chars) |
| `description` | `Description for event` | Poll and event description (max 900 chars) |
| `dm` | Command author | Member who constrains eligible **days** when they vote |
| `weeks` | `2` | 1–4 seven-day blocks, starting **seven calendar days** after the poll is created |
| `days` | `thur,fri,sat,sun` | 1–4 comma-separated weekdays |
| `times` | `2pm,4pm,6pm` | 1–12 comma-separated local times, also accepts `4:15pm` or `18:30` |
| `poll_duration` | `3` | Poll duration in days, 3–7 |
| `attendee_limit` | `12` | Maximum confirmed bot-managed RSVP spots, 1–99 |
| `poll_after_event` | `3` | Hours after an event ENDS before new poll, 3–24 |
| `reminder_before_event` | `6` | Hours before an event STARTS to send a reminder, 3–24 |
| `ping_role` | `@everyone` | Ping on each poll, event and reminder |
| `event_duration` | `4` | Event hours, 1–24 |
| `/reset_scheduler` | — | Clear active posts/events and start a recurring poll using saved settings, or defaults |
| `/end_scheduler` | — | Clear active posts/events and stop the loop |
| `/once_scheduler` | — | Clear active posts/events, poll once and schedule exactly one event (saved settings, or defaults) |

Example: `/start_scheduler title:Dragon of Icespire Peak days:fri,sat times:4pm,6:30pm weeks:2 poll_duration:3 attendee_limit:6`

**Voting behavior choices:** Your original description says the *day* with the most votes wins, while also asking for times. This implementation picks the winning **day** using unique voters, then the winning **time** within that day. Auto-RSVPs are limited to people who voted for that exact winning slot rather than people available only at some other time. The DM's vote determines eligible days, not necessarily times. This is easy to change in `logic.choose_slot` if your group wants another rule.

**Timezone:** The bot uses `TZ_NAME=America/Chicago` by default. Change it to an [IANA timezone](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones) appropriate to your server **before starting**. Discord timestamps display in each viewer's local timezone, while the voting grid uses the configured server timezone. A nonexistent DST time is excluded; an ambiguous fall-back time uses the first occurrence.

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate  # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.example .env
# Edit .env, then export its variables. This app does not automatically read .env.
set -a; source .env; set +a   # macOS/Linux
python bot.py
```

Set `GUILD_ID` to your Discord server ID while testing to make slash commands appear quickly. Without `GUILD_ID`, global commands are synchronized and may take longer to appear. Database file defaults to `./data/scheduler.db` for local runs.

## Put it on GitHub

Open PowerShell, run `winget search --id Microsoft.PowerShell` & `winget install --id Git.Git -e --source winget`.

Create an empty repository named `dnd-with-scheduler`, then in the unpacked project directory:

```bash
git init
git add .
git commit -m "Initial D&D scheduling bot"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/dnd-with-scheduler.git
git push -u origin main
```

The `.gitignore` excludes tokens, `.env`, and SQLite files. GitHub Actions automatically runs unit tests and syntax checks on push and pull requests.

## Deploy 24/7 to Fly.io

Install the [Fly CLI](https://fly.io/docs/flyctl/install/), log in (`fly auth login`), and create a Fly app. **Fly.io hosting and persistent volumes can incur charges**; an always-on Machine is not a free sleeping web app.

1. In `fly.toml`, replace `your-unique-dnd-scheduler` with an available globally unique Fly app name. Change `primary_region = "ord"` if needed; keep the volume in the same region.
2. From the project directory, run the following commands, replacing the name:

```bash
fly auth login
fly apps create YOUR_UNIQUE_APP_NAME
fly volumes create scheduler_data --region ord --size 1 -a YOUR_UNIQUE_APP_NAME
fly secrets set DISCORD_TOKEN=YOUR_SECRET_DISCORD_BOT_TOKEN GUILD_ID=YOUR_DISCORD_SERVER_ID -a YOUR_UNIQUE_APP_NAME
fly deploy -a YOUR_UNIQUE_APP_NAME
fly scale count 1 -a YOUR_UNIQUE_APP_NAME
fly logs -a YOUR_UNIQUE_APP_NAME
```

`GUILD_ID` is recommended, not required. To override timezone or channel lookup, set additional secrets with `fly secrets set TZ_NAME=... SCHEDULING_CHANNEL_ID=... FORUM_CHANNEL_ID=... -a YOUR_UNIQUE_APP_NAME`.

The `fly.toml` intentionally declares **no HTTP service**. This is a long-running Discord Gateway worker, so there is no web address or health-check route to ping, and no incoming traffic to wake the app. Keep **exactly one Machine** running: more than one copy of this gateway process on the same DB would cause duplicate timer actions and SQLite-volume problems. A 1 GB `scheduler_data` volume mounted at `/data` preserves the DB between deployments and restarts. Fly volumes are tied to their region/Machine and aren't automatically backed up: consider periodic volume snapshots/backups.

When you change code and push to GitHub, deploy updated code from your local checkout with `fly deploy`. Alternatively, the included **Deploy to Fly.io** GitHub Actions workflow can deploy manually from GitHub's **Actions** tab. First create the Fly app and volume as above, then set the repo secret `FLY_API_TOKEN` (a scoped Fly deploy token). The workflow is intentionally *manual* (`workflow_dispatch`) so pushes do not unexpectedly deploy. **No production token is included** in this project.

## Tests

```bash
python -m unittest discover -s tests -v
python -m compileall -q bot.py logic.py storage.py
```

## Reliability and limitations

- The bot stores scheduling state, votes, RSVP order, and reminder state in SQLite, and checks for due actions every 45 seconds. Restarts will resume the cycle. Network operations are retried on the next tick if they fail, but there is no guarantee of exactly-once message delivery if Discord accepts an operation immediately before a crash.
- Native Discord *Interested* and bot-managed RSVP are **separate systems**. Discord does not let the bot sign other users up to native Scheduled Event subscriptions. The attendee limit applies to the **bot RSVP message**, not the native Discord Event's Interested counter.
- The app is designed for one Fly Machine, not multiple replicas. The SQLite DB is not automatically replicated to other regions.
- The bot uses the first matching `voice-chat`/`VC` voice channel if present. Without one, it makes an external-location Scheduled Event pointing people to the forum post.
- Reset/end remove **current bot-created messages/posts and future native events created by this bot**. They do not delete unrelated server events or historic forum posts from earlier sessions.
- If no votes remain valid at closing time (including if a restart lasts past proposed session times), a new poll starts.
- The simple app uses no web dashboard, automatic Google account writing, manual organizer override, or external reminder service. The Google button opens the player's own calendar prefilled; the iCal button delivers a downloadable file.
- System notices and scheduling depend on the Fly Machine staying healthy and your Discord/Fly permissions remaining valid. Use `fly logs` to investigate failures.

## Project layout

```text
bot.py                 Discord slash commands, views, recurring worker
logic.py               Date selection, tie-breaking, calendar links and .ics
storage.py             Persistent SQLite schedule, votes, RSVPs
Dockerfile             Python runtime for Fly.io
fly.toml               One 24/7 Machine and persistent volume
requirements.txt       Pinned dependency
.env.example           Environment variable example
.github/workflows/     Unit-test CI + optional manual Fly deployment
 tests/                 Pure logic and storage tests
```

License: MIT.
