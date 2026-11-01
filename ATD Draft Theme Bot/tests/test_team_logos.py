import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock
import test_readiness
import db
import team_logos


class Emoji:
    def __init__(self, name, id=123): self.name, self.id = name, id
    def __str__(self): return f'<:{self.name}:{self.id}>'


class LogoTests(unittest.TestCase):
    def bot(self, names, external=()):
        local = [Emoji(name, i+1) for i, name in enumerate(names)]
        return Mock(get_guild=Mock(return_value=NS(emojis=local)), emojis=local+list(external))

    def test_shorthand_resolves_to_real_discord_emoji_and_prefers_own_server(self):
        bot = self.bot(['Heat'], [Emoji('Heat', 999)])
        self.assertEqual(team_logos.resolve(bot, 1, ':Heat:'), '<:Heat:1>')

    def test_real_and_unicode_logos_are_unchanged(self):
        bot = self.bot(['Heat'])
        for logo in ('<:Heat:1>', '<a:Heat:1>', '\U0001f985'):
            self.assertEqual(team_logos.resolve(bot, 1, logo), logo)

    def test_team_alias_and_case_and_discord_name_suffix_are_supported(self):
        self.assertEqual(team_logos.resolve(self.bot(['MIA']), 1, ':Heat:'), '<:MIA:1>')
        self.assertEqual(team_logos.resolve(self.bot(['Heat~3']), 1, ':heat:'), '<:Heat~3:1>')
        self.assertEqual(team_logos.resolve(self.bot(['OKC']), 1, ':Thunder:'), '<:OKC:1>')

    def test_external_logo_is_replaced_with_server_local_version(self):
        self.assertEqual(team_logos.resolve(self.bot(['heat']), 1, '<:Heat:999>'), '<:heat:1>')

    def test_full_team_name_wins_when_local_team_has_multiple_logo_aliases(self):
        self.assertEqual(team_logos.resolve(self.bot(['Milwaukee_Bucks', 'MIL2'], [Emoji('Bucks', 999)]), 1, '<:Bucks:999>'), '<:Milwaukee_Bucks:1>')

    def test_local_team_alias_precedes_exact_but_external_emoji(self):
        self.assertEqual(team_logos.resolve(self.bot(['MIA'], [Emoji('Heat', 999)]), 1, ':Heat:'), '<:MIA:1>')

    def test_unknown_and_ambiguous_aliases_are_rejected(self):
        with self.assertRaises(ValueError): team_logos.resolve(self.bot(['Nets']), 1, ':Heat:')
        with self.assertRaises(ValueError): team_logos.resolve(self.bot(['MIA', 'Heat2']), 1, ':Heat:')


class RepairTests(unittest.IsolatedAsyncioTestCase):
    async def test_repairs_existing_draft_without_changing_roster_budget_or_turn(self):
        with tempfile.TemporaryDirectory() as temp:
            db.DB_PATH = str(Path(temp)/'logos.db'); db.init_db()
            draft = db.create_draft(1, 2, 'anon_budget_draft', 'Test', {})
            db.set_draft_status(draft, 'active')
            db.set_draft_state(draft, dict(live_pick_number=4, teams=[dict(logo=':Heat:', budget_spent=38, member_ids=[1], open_picks=[])]))
            bot = Mock(get_guild=Mock(return_value=NS(emojis=[Emoji('Heat')])), emojis=[Emoji('Heat')])
            await team_logos.repair(bot)
            state = db.get_draft(draft)['state']
            self.assertEqual(state['teams'][0]['logo'], '<:Heat:123>')
            self.assertEqual(state['teams'][0]['budget_spent'], 38)
            self.assertEqual(state['live_pick_number'], 4)
            await team_logos.repair(bot)
            self.assertEqual(db.get_draft(draft)['state'], state)
