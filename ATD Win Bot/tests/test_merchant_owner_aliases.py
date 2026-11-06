"""Historical owner labels must agree with linked-profile merchant queries."""
import unittest

from aliases import DRAFTER_ALIASES
from test_wins import command_namespace


class MerchantOwnerAliasesTests(unittest.TestCase):
    def setUp(self):
        self.ns = command_namespace()
        self.ns['DRAFTER_ALIASES'] = DRAFTER_ALIASES
        self.drafters = [{'name': name} for name in
                         ('TelluricSolid31', 'CeoKelvin', 'CEOOfKobeFans', 'Other GM')]

    def test_solid_historical_titles_resolve_to_linked_identity(self):
        resolve = self.ns['_make_drafter_resolver'](self.drafters)
        for label in ('Solid(Telluric)', 'Solid31 (#1 Military Fellow)',
                      'Solid31(#1 Veteran Fellow)', 'TelluricSolid31'):
            self.assertEqual(resolve(label), 'TelluricSolid31')

    def test_ambiguous_parentheses_remain_unmerged(self):
        resolve = self.ns['_make_drafter_resolver'](self.drafters)
        label = 'Solid(Other GM)'
        self.assertEqual(resolve(label), label)

    def test_actual_sheet_identity_takes_precedence(self):
        resolve = self.ns['_make_drafter_resolver'](
            self.drafters + [{'name': 'Solid(Other GM)'}])
        self.assertEqual(resolve('Solid(Other GM)'), 'Solid(Other GM)')

    def test_case_sensitive_ceo_identity_is_preserved(self):
        resolve = self.ns['_make_drafter_resolver'](self.drafters)
        self.assertEqual(resolve('CEO'), 'CEOOfKobeFans')
        self.assertEqual(resolve('CEO(Old title)'), 'CEOOfKobeFans')

    def test_kawhi_combines_aliases_without_duplicate_cogm_credit(self):
        history = {
            'ATD 100': {'Team A': {'drafters': ['Solid(Telluric)', 'Solid31'],
                                  'players': [{'name': 'Kawhi Leonard'}]}},
            'ATD 101': {'Team B': {'drafters': ['TelluricSolid31'],
                                  'players': [{'name': 'Kawhi Leonard'}]}},
        }
        index = self.ns['_build_player_drafter_index'](self.drafters, history)
        self.assertEqual(index['kawhileonard']['counts'], {'TelluricSolid31': 2})
        self.assertEqual(self.ns['_get_drafter_players'](
            'TelluricSolid31', history, self.drafters), {'Kawhi Leonard': 2})

    def test_unique_decorations_and_typographic_quotes(self):
        resolve = self.ns['_make_drafter_resolver']([
            {'name': name} for name in ('DQ', 'Sam', 'M.', 'Boneless', 'Cruz', 'Hash')])
        for label, expected in [('DQ🦆', 'DQ'), ("Sam'", 'Sam'), ('m•', 'M.'),
                                ('Ashrith’s dirt', 'Boneless'), ('Aura Cruz', 'Cruz'),
                                ('! Ha Sh', 'Hash')]:
            self.assertEqual(resolve(label), expected)

    def test_normalized_collision_and_emoji_only_remain_unknown(self):
        resolve = self.ns['_make_drafter_resolver']([
            {'name': 'A-B'}, {'name': 'AB'}, {'name': 'CeoKelvin'},
            {'name': 'CEOOfKobeFans'}])
        self.assertEqual(resolve('A.B'), 'A.B')
        self.assertEqual(resolve('🦆'), '🦆')
        self.assertEqual(resolve('C.E.O.'), 'C.E.O.')
        self.assertEqual(resolve('CEO'), 'CEOOfKobeFans')
        self.assertEqual(resolve('Ceo'), 'CeoKelvin')

    def test_numeric_owner_needs_verified_link_and_supports_old_label(self):
        resolve = self.ns['_make_drafter_resolver'](
            self.drafters, {'123456789012345678': {'sheet_name': 'Solid(Telluric)'}})
        for label in ('123456789012345678>', '<@123456789012345678>',
                      '<@!123456789012345678>'):
            self.assertEqual(resolve(label), 'TelluricSolid31')
        self.assertEqual(resolve('999999999999999999>'), '999999999999999999>')

    def test_owner_confirmed_historical_names(self):
        resolve = self.ns['_make_drafter_resolver']([
            {'name': name} for name in ('Esan', 'Vishawn', 'Rick')])
        for label, expected in [('fan', 'Esan'), ('Fan(Esan)', 'Esan'),
                                ('ASoapz', 'Vishawn'), ('ASoapz 💫', 'Vishawn'),
                                ('Dark Rick', 'Rick'), ('myst imo', 'Myst')]:
            self.assertEqual(resolve(label), expected)
