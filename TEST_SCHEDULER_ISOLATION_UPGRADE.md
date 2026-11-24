# Test / live scheduler isolation

Before this update, `/test_scheduler` replaced the scheduler for the current
guild, including its votes, windows, and RSVPs. With this update, tests and live
campaigns have **independent SQLite databases**, both stored on the existing
persistent Fly volume:

* `/data/scheduler.db`: live campaigns, per-server preferences, forum poker
* `/data/scheduler.db.test`: test polls, availability windows, RSVPs, and state

Both database files still key records by Discord `guild_id`, so two servers are
isolated even when they use the same test or live cycle numbers.

## Behavior

* `/start_scheduler`, `/reset_scheduler`, `/end_scheduler`, and
  `/scheduler_status` work with **live campaign data only**.
* `/test_scheduler` replaces **only the latest test** in its server. It does not
  delete or restart the live poll or its future scheduled Discord event.
* Live and test polling/RSVP buttons use different `custom_id`s, so test votes
  cannot be submitted to a live poll even when both are cycle 1.
* The 5-second scheduler worker advances both modes; every Discord interaction
  selects its database using a task-local scope. Test runs still do not loop.
* Test and normal polls are posted in the same configured announcement channel,
  each as a distinct message; test events can coexist with campaign events.
* Starting a new test deletes only its previous test messages and native event.
* No existing live database schema or guild preferences are changed.

## Upgrade caveats

* The patch has narrow, exact-match source hunks. Use `git apply --unidiff-zero`
  for both the dry run and the application; do not use `--reject` or force a
  conflicting patch onto a modified local checkout.
* This is **forward-looking** protection. A test run started **before** this
  upgrade may already have overwritten its server's former live scheduler data;
  the new code cannot reconstruct deleted votes or Windows. Check your live
  scheduler status after deployment and reinitialize if needed.
* The test store is another SQLite file on the same volume. **Do not delete the
  Fly volume**; also avoid deleting `/data/scheduler.db.test` while a test is active.
* The old test-mode `dnd:poll:{guild}:{cycle}` components from pre-upgrade tests
  are not re-registered as test components. Start a fresh test after deployment.
  Old test rows in the live database are also ignored by the background worker.
* `DISCORD_TOKEN` is still required. No additional secrets or migrations.

## Install (PowerShell)

```powershell
cd C:\Coding\dnd-with-scheduler
git apply --unidiff-zero --check "$HOME\Downloads\dnd-isolate-test-scheduler.patch"
git apply --unidiff-zero "$HOME\Downloads\dnd-isolate-test-scheduler.patch"
python -m unittest discover -s tests -v
python -m compileall -q bot.py availability_ui.py scheduler_scope.py

git add bot.py availability_ui.py scheduler_scope.py Dockerfile tests/test_once_scheduler.py tests/test_scheduler_isolation.py TEST_SCHEDULER_ISOLATION_UPGRADE.md
git commit -m "Isolate test scheduler from live campaigns per server"
git push
fly deploy -a dnd-with-scheduler
```

After deployment, run `/scheduler_status` on a live server, then run
`/test_scheduler`, vote in the test poll, and check `/scheduler_status` again.
The live poll and its counts should be unchanged. Verify the same in a second
guild if available.
