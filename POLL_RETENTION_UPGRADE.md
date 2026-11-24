# Three-month poll history retention

## Summary

Old polling data is purged **when a new poll cycle is prepared**, not on a timer. Each poll cycle is dated by its **poll deadline**, not by when a person first voted. This is important because carried availability deliberately retains its original vote timestamp.

The cutoff is **three calendar months** before the new poll creation time in UTC (not a fixed 90 days), including month-end handling. Cycles with a deadline *strictly before* that cutoff are eligible. The new poll and newer cycles are not removed.

## What is deleted

For eligible **older** cycles within the same server and the same live/test database:

- Availability votes (`votes`)
- Saved availability ranges (`availability_windows`)
- RSVPs (`rsvps`)
- The cycle's retention-tracking record (`poll_cycles`)

The active campaign configuration, newer votes, current/past forum thread posts, `/poke_forums` subscriptions, server preferences, and service status are not deleted by retention. Existing reset/end behavior is unchanged.

## When it runs

`Store.put()` applies retention as part of creating a `poll_preparing` record with a poll deadline; this is how normal poll preparation currently works. The update is part of an atomic SQLite transaction. There is **no hourly/daily periodic cleanup**.

Live campaigns use `/data/scheduler.db` and isolated `/test_scheduler` runs use `/data/scheduler.db.test` on the same Fly volume. Each cleans only its own database and the server starting the poll.

## Migration of historical data

The updated `Store` creates a `poll_cycles` table automatically; **no manual migration** is required.

- The *currently recorded* poll's real deadline is backfilled if available.
- Prior cycles have no stored poll deadline. Rather than guessing from old vote timestamps and potentially deleting valid data, their **first-seen upgrade time** becomes a conservative retention date. This means old legacy data may remain until at least three months after this upgrade and a newer poll has been created. It will not be held indefinitely if regular polls continue.

## SQLite file size

Deleting historical rows lets SQLite reuse their database pages. SQLite does **not necessarily reduce the on-disk database file size immediately**; that is normal and does not mean the rows are still present. This change does not run `VACUUM`, since it can interrupt an active service.

## Install (Windows PowerShell)

From `C:\Coding\dnd-with-scheduler`, after downloading `dnd-three-month-poll-retention.patch` into Downloads:

```powershell
cd C:\Coding\dnd-with-scheduler

git apply --check "$HOME\Downloads\dnd-three-month-poll-retention.patch"
git apply "$HOME\Downloads\dnd-three-month-poll-retention.patch"

python -m unittest discover -s tests -v
python -m compileall -q storage.py bot.py availability.py
```

If the full test suite passes:

```powershell
git add storage.py tests/test_poll_retention.py POLL_RETENTION_UPGRADE.md
git commit -m "Prune expired poll results when new polls are prepared"
git push
fly deploy -a dnd-with-scheduler
```

For extra safety before deploying a change that removes old poll data, back up the Fly SQLite volume. The cleanup is destructive for historical vote/RSVP rows once they are past the retention threshold. The separate test database is included if you back up the entire `/data` volume.

## Verification

The six focused retention tests passed in a local reconstructed source environment. The patch was checked against exact GitHub source snippets. Run the complete repo test suite and test a new poll in Discord before relying on the behavior in production.
