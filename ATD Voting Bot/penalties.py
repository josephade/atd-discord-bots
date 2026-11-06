"""Counterfactual records; never change official standings or saved results."""
from decimal import Decimal, ROUND_HALF_UP
import os
import ipaddress
import socket
from urllib.parse import urlparse
import aiohttp
from reveal import record_text
from votes import normalize


class LedgerResolver(aiohttp.abc.AbstractResolver):
    """Optional assigned ingress address when local DNS caches a new host as absent.

    HTTPS still verifies the original URL hostname and uses it for SNI/Host.
    """
    def __init__(self, url, fallback=''):
        self.hostname = urlparse(url).hostname
        self.fallback = str(ipaddress.IPv4Address(fallback)) if fallback else ''
        self.delegate = aiohttp.resolver.ThreadedResolver()

    async def resolve(self, host, port=0, family=socket.AF_INET):
        try:
            return await self.delegate.resolve(host, port, family)
        except OSError:
            if host != self.hostname or not self.fallback:
                raise
            return [dict(hostname=host, host=self.fallback, port=port,
                         family=socket.AF_INET, proto=0, flags=0)]

    async def close(self):
        await self.delegate.close()


async def fetch_ledger(theme_draft_id, guild_id):
    url = os.getenv('THEME_PENALTIES_URL', '').rstrip('/')
    key = os.getenv('THEME_PENALTIES_KEY', '')
    if not url.startswith('https://') or not key:
        raise ValueError('The Theme Bot penalty connection is not configured.')
    resolver = LedgerResolver(url, os.getenv('THEME_PENALTIES_IP', ''))
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12),
                                   connector=aiohttp.TCPConnector(resolver=resolver)) as session:
        async with session.get(f'{url}/penalties/{int(theme_draft_id)}',
                               params={'guild': str(guild_id)},
                               headers={'Authorization': 'Bearer ' + key}) as response:
            if response.status != 200:
                raise ValueError('Could not read the linked suspect ledger. Try again shortly.')
            data = await response.json()
    if data.get('theme_draft_id') != int(theme_draft_id) or data.get('guild_id') != int(guild_id):
        raise ValueError('The suspect ledger does not match this draft/server.')
    return data


def carryover_details(result, draft):
    raw = max(Decimal(0), -Decimal(result['wins']))
    amount = int(raw.quantize(Decimal('1'), rounding=ROUND_HALF_UP))
    if not amount:
        return {}
    return dict(carryover_wins=amount, carryover_raw_wins=str(raw),
                carryover_to_draft=str(int(draft) + 1))


def comparison_lines(result, ledger, draft=None):
    ids = set(result.get('gm_ids', []))
    # Exact team identity from Theme Bot supports anonymous standings whose
    # GM cell is intentionally blank. Never use fuzzy team-name matching.
    matches = [team for team in ledger['teams'] if team.get('team_name') and
               normalize(team['team_name']) == normalize(result['team'])]
    if not matches and ids:
        matches = [team for team in ledger['teams'] if ids & set(team['user_ids'])]
    if len(matches) != 1 or ids and not ids <= set(matches[0]['user_ids']):
        raise ValueError('The revealed GM mapping does not match one team in the suspect ledger.')
    team = matches[0]
    caught, market, quantity = (int(team.get(key, 0)) for key in
                               ('caught_wins', 'market_wins', 'bought_guesses'))
    if min(caught, market, quantity) < 0 or market != quantity * 5:
        raise ValueError('The suspect ledger has inconsistent deductions.')
    wins, losses = Decimal(result['wins']), Decimal(result['losses'])
    if losses < caught + market or wins + caught + market < 0:
        raise ValueError('The sheet record and suspect deductions do not reconcile. Check that the sheet includes these deductions.')

    def restored(amount):
        return record_text(dict(wins=str(wins + amount), losses=str(losses - amount)))

    lines = []
    if market:
        noun = 'guess' if quantity == 1 else 'guesses'
        lines.append(f'Without buying {quantity} suspect {noun}: **{restored(market)}**.')
    if caught:
        lines.append(f'Without being caught by a correct suspect: **{restored(caught)}**.')
    if caught and market:
        lines.append(f'Without either deduction: **{restored(caught + market)}**.')
    if wins < 0:
        if draft is None:
            raise ValueError('Specify the ATD number to record the remaining deduction.')
        carry = carryover_details(result, draft)
        if carry:
            lines.append(f'The remaining **{carry["carryover_wins"]}-win deduction carries over to ATD {carry["carryover_to_draft"]}**.')
    return lines
