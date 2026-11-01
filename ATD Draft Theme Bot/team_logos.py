"""Resolve typed custom emoji shorthand before publishing draft picks."""
import re
import logging
import db
import discord
from emoji_map import EMOJI_TEAM_MAP
from themes.base import draft_lock

_NAMES = {re.sub(r'~\d+$', '', k).strip(':').casefold(): v for k, v in EMOJI_TEAM_MAP.items()}
_NAMES['thunder'] = 'Oklahoma City Thunder'
_GUILD_EMOJIS = {}


async def refresh(bot, guild_id):
    import discord
    guild = bot.get_guild(guild_id)
    if guild is None:
        guild = await bot.fetch_guild(guild_id)
    if isinstance(guild, discord.Guild):
        _GUILD_EMOJIS[guild_id] = await guild.fetch_emojis()


def resolve(bot, guild_id, logo):
    match = re.fullmatch(r':([A-Za-z0-9_~]+):', logo.strip())
    full = re.fullmatch(r'<a?:([A-Za-z0-9_~]+):(\d+)>', logo.strip())
    if not match and not full:
        return logo
    wanted = (match or full).group(1).casefold()
    guild = bot.get_guild(guild_id)
    local = _GUILD_EMOJIS.get(guild_id, list(getattr(guild, 'emojis', [])) if guild else [])
    if full and any(e.id == int(full.group(2)) for e in local):
        return logo
    base = re.sub(r'~\d+$', '', wanted)
    team = _NAMES.get(base)
    pools = [local, list(bot.emojis)]
    for pool in pools:
        exact = [e for e in pool if e.name.casefold() == wanted]
        if len(exact) == 1:
            return str(exact[0])
        if len(exact) > 1:
            raise ValueError(f'More than one emoji matches {logo}. Select the actual emoji in Discord.')
        matches = [e for e in pool if re.sub(r'~\d+$', '', e.name).casefold() == base or
                   team is not None and _NAMES.get(re.sub(r'~\d+$', '', e.name).casefold()) == team]
        if len(matches) == 1:
            return str(matches[0])
        if len(matches) > 1 and team:
            team_key = re.sub(r'[^a-z0-9]', '', team.casefold())
            full_name = [e for e in matches if re.sub(r'[^a-z0-9]', '', e.name.casefold()) == team_key]
            if len(full_name) == 1:
                return str(full_name[0])
        if len(matches) > 1:
            raise ValueError(f'More than one emoji matches {logo}. Select the actual emoji in Discord.')
    raise ValueError(f'I cannot find the custom emoji {logo}. Select the real team emoji from Discord autocomplete and try again. No teams were changed.')


async def repair(bot):
    for draft in db.list_drafts():
        if draft['theme'] not in ('anon_draft', 'anon_budget_draft') or draft['status'] not in ('setup', 'active', 'paused'):
            continue
        try:
            await refresh(bot, draft['guild_id'])
        except discord.HTTPException:
            logging.getLogger(__name__).exception('Could not refresh server emojis for draft %s', draft['id'])
            continue
        async with draft_lock(draft['id']):
            state = db.get_draft(draft['id'])['state']
            changed = False
            for team in state.get('teams', []):
                try:
                    logo = resolve(bot, draft['guild_id'], team['logo'])
                except ValueError:
                    logging.getLogger(__name__).warning('Unresolved team logo in draft %s: %s', draft['id'], team['logo'])
                    continue
                if logo != team['logo']:
                    team['logo'] = logo
                    changed = True
            if changed:
                db.update_draft_state(draft['id'], {'teams': state['teams']})
                logging.getLogger(__name__).info('Repaired custom team logos in draft %s', draft['id'])
