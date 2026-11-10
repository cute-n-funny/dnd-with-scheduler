"""DM availability restrictions must be scoped to Sunday–Saturday calendar weeks.

Pure scheduling and image checks: no Discord token, gateway, or SQLite required.
"""
from datetime import datetime, timedelta, timezone
from io import BytesIO
import unittest

from PIL import Image, ImageColor

from heatmap import GREEN, LIGHT_GREEN, RED, ORANGE, render_heatmaps
from logic import choose_slot, slot_keys


def votes(slot, people):
    return [{'slot': slot, 'user_id': p,
             'created_at': f'2026-11-25T10:00:{i:02d}+00:00'}
            for i, p in enumerate(people)]


class DMWeekScopingTests(unittest.TestCase):
    dm = 99
    # 2026-12-06 and 2026-12-13 are consecutive Sundays.
    sun1 = '2026-12-06T14:00'
    mon1 = '2026-12-07T14:00'
    fri1 = '2026-12-11T14:00'
    sat1 = '2026-12-12T14:00'
    sun2 = '2026-12-13T14:00'
    mon2 = '2026-12-14T14:00'

    def test_dm_votes_week_one_only_week_two_remains_eligible(self):
        ballot = (votes(self.sun1, [self.dm]) + votes(self.mon1, range(1, 9))
                  + votes(self.sun2, range(11, 19)))
        winner, users = choose_slot(ballot, [self.sun1, self.mon1, self.sun2], self.dm,
                                    attendee_limit=8, min_votes=3)
        self.assertEqual(winner, self.sun2)
        self.assertEqual(users, list(range(11, 19)))

    def test_dm_votes_week_two_only_week_one_remains_eligible(self):
        ballot = votes(self.mon1, range(1, 9)) + votes(self.sun2, [self.dm])
        winner, _ = choose_slot(ballot, [self.mon1, self.sun2], self.dm,
                                attendee_limit=8, min_votes=3)
        self.assertEqual(winner, self.mon1)

    def test_dm_votes_both_weeks_dates_constrained_in_each(self):
        ballot = (votes(self.sun1, [self.dm]) + votes(self.mon1, range(1, 9))
                  + votes(self.sun2, [self.dm, *range(11, 19)]))
        winner, users = choose_slot(ballot, [self.sun1, self.mon1, self.sun2], self.dm,
                                    attendee_limit=8, min_votes=3)
        self.assertEqual(winner, self.sun2)
        self.assertEqual(users[0], self.dm)

    def test_sunday_starts_new_week_even_after_saturday_dm_vote(self):
        ballot = (votes(self.fri1, range(1, 9)) + votes(self.sat1, [self.dm])
                  + votes(self.sun2, range(11, 19)))
        winner, _ = choose_slot(ballot, [self.fri1, self.sat1, self.sun2], self.dm,
                                attendee_limit=8, min_votes=3)
        self.assertEqual(winner, self.sun2)

    def test_no_dm_ballots_all_weeks_unrestricted(self):
        ballot = votes(self.mon1, range(1, 9)) + votes(self.sun2, range(11, 19))
        winner, _ = choose_slot(ballot, [self.mon1, self.sun2], self.dm,
                                attendee_limit=8, min_votes=3)
        self.assertEqual(winner, self.mon1)

    def test_no_week_without_three_distinct_voters_can_win(self):
        ballot = votes(self.sun1, [self.dm]) + votes(self.sun2, [11, 12])
        self.assertEqual(choose_slot(ballot, [self.sun1, self.sun2], self.dm,
                                     attendee_limit=8, min_votes=3), (None, []))

    @staticmethod
    def record():
        # Creation Sunday Nov 29 => candidate slots Dec 6–19, in exactly two weeks.
        created = datetime(2026, 11, 29, 12, tzinfo=timezone.utc)
        config = dict(title='Campaign', dm_id=99, weeks=2,
                      days=[6, 0, 1, 2, 3, 4, 5], times=['14:00'],
                      timezone='UTC', poll_duration=3, attendee_limit=8)
        return {'config': config,
                'poll_deadline': (created + timedelta(days=3)).isoformat()}

    def test_heatmap_week_two_not_red_or_orange_after_week_one_dm_vote(self):
        record = self.record()
        # A week-one DM vote, a different week-one player vote, and week-two votes.
        ballot = (votes(self.sun1, [self.dm]) + votes(self.mon1, [1])
                  + votes(self.sun2, [11, 12, 13]))
        images = render_heatmaps(record, ballot)
        self.assertEqual([name for name, _ in images], [
            'dnd-availability-week-2026-12-06.png',
            'dnd-availability-week-2026-12-13.png'])
        with Image.open(BytesIO(images[0][1])) as week1:
            # Week one: Mon has votes outside DM availability, Tues has none.
            self.assertEqual(week1.getpixel((240, 150)), ImageColor.getrgb(ORANGE))
            self.assertEqual(week1.getpixel((345, 150)), ImageColor.getrgb(RED))
        with Image.open(BytesIO(images[1][1])) as week2:
            # Week two: no DM ballots, so normal voter-count colors apply.
            self.assertEqual(week2.getpixel((135, 150)), ImageColor.getrgb(GREEN))
            self.assertEqual(week2.getpixel((240, 150)), ImageColor.getrgb(LIGHT_GREEN))

    def test_heatmap_dm_windows_also_apply_only_their_week(self):
        record = self.record()
        windows = [
            {'day': '2026-12-06', 'start': '14:00', 'hours': 4, 'user_id': self.dm},
            {'day': '2026-12-13', 'start': '14:00', 'hours': 4, 'user_id': 11},
        ]
        images = render_heatmaps(record, [], windows=windows)
        self.assertEqual(len(images), 2)
        with Image.open(BytesIO(images[0][1])) as week1:
            self.assertEqual(week1.getpixel((240, 150)), ImageColor.getrgb(RED))
        with Image.open(BytesIO(images[1][1])) as week2:
            self.assertEqual(week2.getpixel((135, 150)), ImageColor.getrgb(LIGHT_GREEN))


if __name__ == '__main__':
    unittest.main()
