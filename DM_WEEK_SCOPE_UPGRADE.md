# DM availability now scoped per calendar week

This patch changes how both **heatmap colors** and **winning session dates** apply the DM's votes.

- A week means **Sunday through Saturday** in the date shown by the scheduler (the same grouping as the existing availability UI and heatmap).
- **DM voted somewhere within that week:** only the dates on which the DM submitted availability remain eligible. The heatmap marks other slots as red/orange as before.
- **DM did not vote anywhere within that week:** other players' votes remain eligible; heatmap uses ordinary green/light-green colors rather than marking DM absence as red/orange.
- A DM vote in Week 1 does **not** restrict dates in Week 2, and vice versa.
- Distinct-voter minimum, attendee limit, tie breaks, RSVP ordering, and all saved vote data remain unchanged.
- The existing behavior of **not generating an image for a week without any votes/windows** remains unchanged. If Week 2 has no votes from anyone, there will still be no Week 2 heatmap until someone votes.

## Windows PowerShell

Download `dnd-dm-availability-per-week.patch` to Downloads and run:

```powershell
cd C:\Coding\dnd-with-scheduler
git apply --check "$HOME\Downloads\dnd-dm-availability-per-week.patch"
git apply "$HOME\Downloads\dnd-dm-availability-per-week.patch"
python -m unittest discover -s tests -v
python -m compileall -q logic.py heatmap.py bot.py

git add logic.py heatmap.py tests/test_vote_rules_v2.py tests/test_dm_week_scoping.py DM_WEEK_SCOPE_UPGRADE.md
git commit -m "Scope DM availability to each calendar week"
git push
fly deploy -a dnd-with-scheduler
```

If `git apply --check` fails, do not force the patch. Share the error so it can be adapted to your checkout. No database migration is required.
