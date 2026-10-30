import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import random
import unittest

from logic import (choose_slot, event_datetimes, google_calendar_url, make_ics,
                   parse_days, parse_times, slot_keys)
from storage import Store

C = {"weeks": 2, "days": [3, 4, 5, 6], "times": ["14:00", "16:00", "18:00"],
     "timezone": "America/Chicago", "event_duration": 4}


class SchedulingTests(unittest.TestCase):
    def test_day_parser(self):
        self.assertEqual(parse_days("thur,fri,sat,sun"), [3,4,5,6])
        with self.assertRaises(ValueError): parse_days("fri,fri")
        with self.assertRaises(ValueError): parse_days("monday,tue,wed,thu,fri")

    def test_time_parser(self):
        self.assertEqual(parse_times("2pm,4:15pm,6:30pm"), ["14:00", "16:15", "18:30"])
        self.assertEqual(parse_times("12am,12pm,00:30"), ["00:00", "12:00", "00:30"])
        with self.assertRaises(ValueError): parse_times("25:00")

    def test_weeks_start_seven_days_ahead(self):
        now = datetime(2026, 10, 30, 17, tzinfo=timezone.utc)
        slots = slot_keys(C, now, now + timedelta(days=3))
        self.assertTrue(slots)
        self.assertEqual(len(slots), 2*4*3)
        self.assertTrue(all(s[:10] >= '2026-11-06' for s in slots))

    def test_day_by_unique_voters_then_time(self):
        slots = ["2026-11-06T14:00", "2026-11-06T16:00", "2026-11-07T14:00"]
        votes = [
          {"user_id":1,"slot":slots[0],"created_at":"1"},
          {"user_id":1,"slot":slots[1],"created_at":"2"},
          {"user_id":2,"slot":slots[1],"created_at":"3"},
          {"user_id":3,"slot":slots[2],"created_at":"4"},
          {"user_id":4,"slot":slots[2],"created_at":"5"},
          {"user_id":5,"slot":slots[2],"created_at":"6"},
        ]
        chosen, users = choose_slot(votes, slots, dm_id=1, rng=random.Random(1))
        self.assertEqual(chosen, slots[1])  # DM constrains date; time 16:00 most popular
        self.assertEqual(users, [1,2])
        chosen, users = choose_slot(votes, slots, dm_id=999, rng=random.Random(1))
        self.assertEqual(chosen, slots[2])
        self.assertEqual(users, [3,4,5])

    def test_no_votes(self):
        self.assertEqual(choose_slot([], ["2026-01-01T14:00"], 1), (None, []))

    def test_calendar_utc(self):
        start, end = event_datetimes('2026-11-06T14:00', C)
        self.assertEqual(start.isoformat(), '2026-11-06T20:00:00+00:00')
        self.assertEqual(end-start, timedelta(hours=4))
        self.assertIn("action=TEMPLATE", google_calendar_url("D&D", "Prep", start, end))
        self.assertIn(b"BEGIN:VEVENT", make_ics("D&D", "Line 1", start, end, "uid"))
        ics = make_ics("🐉"*100, "long"*100, start, end, "uid")
        self.assertTrue(all(len(line) <= 75 for line in ics.split(b"\r\n")))
        self.assertLessEqual(len(google_calendar_url("🐉"*100, "long"*100, start, end)), 500)

    def test_store_rsvp_waitlist_promotion(self):
        async def exercise(store):
            await store.put(123, dict(C), 1, "event")
            await store.set_initial_rsvps(123, 1, [10, 20, 30], 2)
            self.assertEqual([r["status"] for r in await store.rsvps(123,1)],
                             ["confirmed", "confirmed", "waitlist"])
            status, promoted = await store.toggle_rsvp(123, 1, 20, 2)
            self.assertEqual((status, promoted), ("removed", 30))
            self.assertEqual({r["user_id"] for r in await store.rsvps(123,1)
                              if r["status"] == "confirmed"}, {10,30})
        with TemporaryDirectory() as folder:
            asyncio.run(exercise(Store(str(Path(folder)/'scheduler.db'))))

if __name__ == '__main__':
    unittest.main()
