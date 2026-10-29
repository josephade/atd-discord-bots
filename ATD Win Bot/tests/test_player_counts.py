from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock

from test_wins import command_namespace, typing


def fixtures():
    lottos = {}
    for num in range(1, 7):
        spelling = 'Michael Porter Jr' if num <= 5 else 'Michael Porter Jr.'
        lottos[f'ATD {num}'] = {'Team': {'drafters': ['Mondon'], 'players': [{'name': spelling}]}}
    for num in range(7, 10):
        lottos[f'ATD {num}'] = {'Team': {'drafters': ['Other GM'], 'players': [{'name': 'Michael Porter Jr.'}]}}
    return lottos


class PlayerIdentityTests(unittest.TestCase):
    def setUp(self):
        self.ns = command_namespace()
        self.lottos = fixtures()
        self.drafters = [{'name': 'Mondon', 'draft_wins': {}}, {'name': 'Other GM', 'draft_wins': {}}]

    def test_mondon_six_porter_drafts_are_one_bucket(self):
        index = self.ns['_build_player_drafter_index'](self.drafters, self.lottos)
        key = self.ns['_player_key']('Michael Porter Jr.')
        self.assertEqual(len(index), 1)
        self.assertEqual(index[key]['counts'], {'Mondon': 6, 'Other GM': 3})
        self.assertEqual(sum(self.ns['_get_drafter_players']('Mondon', self.lottos).values()), 6)

    def test_sheet_headings_are_not_players_in_leaderboards_or_profile_counts(self):
        names = ['Eastern Conference', 'Western Conference', 'Division 8', 'Group G', 'Atlantic Division',
                 'Players', 'Year', 'Total', 'Bob Cousy', 'David West', 'World B. Free']
        data = {'ATD 1': {'Team': {'drafters': ['Mondon'], 'players': [{'name': n} for n in names]}}}
        rows = [{'name': 'Mondon', 'draft_wins': {1: 52}}]
        expected = {'Bob Cousy', 'David West', 'World B. Free'}
        index = self.ns['_build_player_drafter_index'](rows, data)
        self.assertEqual({b['display'] for b in index.values()}, expected)
        self.assertEqual(set(self.ns['_get_drafter_players']('Mondon', data, rows)), expected)
        ranking, _ = self.ns['_build_player_win_totals'](data, rows)
        self.assertEqual({p['name'] for p in ranking}, expected)

    def test_silver_and_silveryo_have_seven_fred_brown_drafts(self):
        import runpy
        from pathlib import Path
        self.ns['DRAFTER_ALIASES'] = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'aliases.py'))['DRAFTER_ALIASES']
        data = {f'ATD {i}': {'Team': {'drafters': ['Silveryo' if i <= 5 else 'Silver'],
                                   'players': [{'name': 'Fred Brown'}]}} for i in range(1, 8)}
        rows = [{'name': 'Silveryo'}, {'name': 'Silver'}]
        index = self.ns['_build_player_drafter_index'](rows, data)
        self.assertEqual(index[self.ns['_player_key']('Fred Brown')]['counts'], {'Silveryo': 7})
        self.assertEqual(self.ns['_get_drafter_players']('Silver', data, rows), {'Fred Brown': 7})
        data['ATD 7']['Team']['drafters'] = ['Silver', 'Silveryo']
        self.assertEqual(self.ns['_build_player_drafter_index'](rows, data)[self.ns['_player_key']('Fred Brown')]['counts'], {'Silveryo': 7})

    def test_normalises_punctuation_accents_hyphens_and_spacing(self):
        for names in [('Michael Porter Jr', 'Michael Porter Jr.'),
                      ('Shaquille O’Neal', "Shaquille O'Neal", 'Shaquille O Neal'),
                      ('Karl-Anthony Towns', 'Karl Anthony-Towns'),
                      ('Manu Ginóbili', 'Manu Ginobili'), ('J.J. Redick', 'JJ Redick', 'J J Redick')]:
            with self.subTest(names=names):
                self.assertEqual(len({self.ns['_player_key'](n) for n in names}), 1)

    def test_optional_senior_keeps_father_and_son_distinct(self):
        key = self.ns['_player_key']
        self.assertEqual(key('Tim Hardaway Sr.'), key('Tim Hardaway'))
        self.assertNotEqual(key('Tim Hardaway Jr.'), key('Tim Hardaway Sr.'))
        self.assertNotEqual(key('Glen Rice'), key('Glen Rice Jr'))

    def test_once_per_roster_and_full_credit_for_each_co_gm(self):
        data = {'ATD 1': {'Team': {'drafters': ['old name', 'New Name', 'Mondon'], 'players': [
            {'name': 'Michael Porter Jr'}, {'name': 'Michael Porter Jr.'}, {'name': ''}, {'name': 'Group A'}]}}}
        rows = [{'name': 'New Name'}, {'name': 'Mondon'}]
        index = self.ns['_build_player_drafter_index'](rows, data)
        self.assertEqual(next(iter(index.values()))['counts'], {'New Name': 1, 'Mondon': 1})
        for drafter in rows:
            self.assertEqual(list(self.ns['_get_drafter_players'](drafter['name'], data).values()), [1])

    def test_all_profile_counts_match_merchant_index(self):
        index = self.ns['_build_player_drafter_index'](self.drafters, self.lottos)
        for drafter in self.drafters:
            profile = self.ns['_get_drafter_players'](drafter['name'], self.lottos)
            for player, count in profile.items():
                self.assertEqual(index[self.ns['_player_key'](player)]['counts'][drafter['name']], count)

    def test_adp_lookup_uses_same_player_identity(self):
        self.ns['ADP_MAP'] = {'michael porter jr.': 273.2, "shaquille o'neal": 3.7}
        self.assertEqual(self.ns['_lookup_adp_player']('Michael Porter Jr')[0], 'michael porter jr.')
        self.assertEqual(self.ns['_get_adp']('Shaquille O’Neal'), 3.7)

    def test_exact_gm_is_not_overwritten_by_another_gms_alias(self):
        self.ns['DRAFTER_ALIASES'] = {'old name': 'New Name'}
        rows = [{'name': 'New Name'}, {'name': 'Someone(old name)'}]
        resolve = self.ns['_make_drafter_resolver'](rows)
        self.assertEqual(resolve('old name'), 'New Name')
        self.assertEqual(resolve('New Name'), 'New Name')

    def test_case_sensitive_ceo_override_stays_separate(self):
        self.ns['DRAFTER_ALIASES'] = {'ceokelvin': 'Ceo', 'kelvin': 'CeoKelvin'}
        resolve = self.ns['_make_drafter_resolver']([{'name': 'CeoKelvin'}, {'name': 'CEOOfKobeFans'}])
        self.assertEqual(resolve('Ceo'), 'CeoKelvin')
        self.assertEqual(resolve('CEO'), 'CEOOfKobeFans')

    def test_ambiguous_alias_is_not_arbitrarily_assigned(self):
        resolve = self.ns['_make_drafter_resolver']([{'name': 'First(Sam)'}, {'name': 'Second(Sam)'}])
        self.assertEqual(resolve('Sam'), 'Sam')


class MerchantCommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.ns = command_namespace()
        self.ns.update(_get_raw=AsyncMock(return_value=['data']),
                       _parse=lambda raw: ([{'name': 'Mondon', 'draft_wins': {}}, {'name': 'Other GM', 'draft_wins': {}}], [], []),
                       _load_lottos=fixtures, MEDALS=['1.', '2.', '3.'],
                       ADP_MAP={'michael porter jr.': 273.2, 'michael porter jr': 273.2})
        self.ctx = NS(typing=typing, send=AsyncMock(), message=NS(mentions=[]))

    async def test_both_porter_spellings_show_mondon_first_with_six(self):
        for query in ('Michael Porter Jr.', 'Michael Porter Jr'):
            await self.ns['cmd_merchant'](self.ctx, args=query)
            desc = self.ctx.send.call_args.kwargs['embed'].description
            self.assertIn('Mondon — ×6', desc.splitlines()[0])

    async def test_browse_has_one_row_per_player(self):
        await self.ns['cmd_merchant'](self.ctx, args='')
        lines = self.ctx.send.call_args.kwargs['view'].lines
        self.assertEqual(len(lines), 1)
        self.assertIn('Mondon (×6)', lines[0])


if __name__ == '__main__':
    unittest.main()
