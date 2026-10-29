import unittest

from draft_history import player_key, summarize_team_sheet


class TeamSheetHistoryTests(unittest.TestCase):
    def test_draft_range_and_multiple_tabs(self):
        roster = [['Team', 'Year'], ['Tim Hardaway Sr.', '1990-91']]
        summary = summarize_team_sheet({
            'ATD 14': roster,
            'ATD 15(Standard)': roster,
            'ATD 100 - West': roster,
            'ATD 100 - East': roster,
            'ATD 112 - Auto Draft': roster,
            'ATD 113 - Draft': roster,
            'Template': roster,
        })
        self.assertEqual(summary['draft_numbers'], [15, 100, 112])
        self.assertEqual(summary['total_drafts'], 3)
        self.assertEqual(summary['player_counts']['tim hardaway'], 4)

    def test_two_peak_roster_with_gap_and_eleventh_row(self):
        roster = [['Team', 'Off', 'Def'], ['Tim Hardaway', '1990-91', '1996-97']]
        roster += [[] for _ in range(9)]
        roster += [['Phil Smith', '1975-76', '1974-75']]
        summary = summarize_team_sheet({'ATD 46(2 Peaks)': roster})
        self.assertEqual(summary['player_counts']['tim hardaway'], 1)
        self.assertEqual(summary['player_counts']['phil smith'], 1)

    def test_names_duplicates_and_non_roster_mentions(self):
        roster = [
            ['Tim Hardaway Sr.', 'Notes'],
            ['Team', 'Year'],
            ['Tim Hardaway', '1990-91'],
            ['Tim Hardaway Sr.', '1996-97'],
            ['Tim Hardaway Jr.', '2020-21'],
            ['Other team', 'Year'],
            ['Tim Hardaway Sr', '1990-91'],
        ]
        counts = summarize_team_sheet({'ATD 15': roster})['player_counts']
        self.assertEqual(counts[player_key('Tim Hardaway Sr.')], 2)
        self.assertEqual(counts[player_key('Tim Hardaway Jr.')], 1)

    def test_empty_and_non_basketball_drafts_still_count_in_total(self):
        summary = summarize_team_sheet({
            'ATD 33': [['Team', 'Year']],
            'ATD 48(Football)': [['Position', 'Team'], ['GK', 'Dino Zoff']],
        })
        self.assertEqual(summary['total_drafts'], 2)
        self.assertEqual(summary['player_counts'], {})


if __name__ == '__main__':
    unittest.main()
