"""Anonymous GM guesses, bounded attempts, and a persisted penalty ledger."""
import re

import db
import themes
from emoji_map import EMOJI_TEAM_MAP
from themes.base import DraftContext, draft_lock

ANON_THEMES = frozenset(('anon_draft', 'anon_budget_draft', 'partner_draft'))
ALLOWED_CHANNEL_IDS = frozenset((934052158532378634, 1533242104304631969))
CONSUME_WRONG_ATTEMPTS = True
ALLOW_REPEAT_CATCHES = False
_CUSTOM = re.compile(r'<a?:([^:<>]+):(\d+)>')
_ALIASES = {re.sub(r'~\d+$', '', name).casefold(): team.casefold() for name, team in EMOJI_TEAM_MAP.items()}


def teams(draft):
    state = draft['state']
    if draft['theme'] == 'partner_draft':
        return [{'logo': p.get('logo', f'🏀 Team {i + 1}'), 'member_ids': [p['a'], p['b']]}
                for i, p in enumerate(state.get('pairs', []))]
    return state.get('teams', [])


def _emoji_name(raw):
    match = _CUSTOM.fullmatch(raw)
    name = match.group(1) if match else raw.strip(':')
    return re.sub(r'~\d+$', '', name).casefold()


def logo_matches(raw, logo):
    raw, logo = raw.strip(), logo.strip()
    first, second = _CUSTOM.fullmatch(raw), _CUSTOM.fullmatch(logo)
    if first and second:
        return first.group(2) == second.group(2)
    if raw.replace('\ufe0f', '') == logo.replace('\ufe0f', ''):
        return True
    name, other = _emoji_name(raw), _emoji_name(logo)
    return name == other or (name in _ALIASES and _ALIASES.get(name) == _ALIASES.get(other))


def resolve_draft(guild_id, channel_id, user_id):
    drafts = [d for d in db.list_drafts(guild_id=guild_id)
              if d['theme'] in ANON_THEMES and d['status'] in ('active', 'paused', 'complete')
              and db.get_drafter_by_user(d['id'], user_id)]
    local = [d for d in drafts if channel_id in (d.get('admin_channel_id'), d['state'].get('timer_channel_id'))]
    running = [d for d in (local or drafts) if d['status'] in ('active', 'paused')]
    if len(running) > 1:
        return None, 'More than one anonymous draft matches. Ask the commissioner to close the extra draft before guessing.'
    if running:
        return running[0], None
    if local or drafts:
        return (local or drafts)[0], None
    return None, 'You must be a GM in an anonymous draft to use `!suspect`.'


def after_final_round(ctx):
    state = ctx.state
    if ctx.draft['status'] == 'complete' or state.get('scheduled_turns_finished'):
        return True
    if ctx.draft['theme'] == 'partner_draft' and state.get('turns'):
        return state.get('turn_index', 0) >= len(state['turns'])
    return themes.get_theme(ctx.draft['theme']).is_pick_phase_complete(ctx)


async def guess(bot, draft_id, message_id, user_id, target_id, emoji):
    async with draft_lock(draft_id):
        ctx = DraftContext(bot, draft_id)
        draft = ctx.draft
        if not draft or draft['theme'] not in ANON_THEMES or draft['status'] not in ('active', 'paused', 'complete'):
            return {'status': 'closed'}
        if draft['state'].get('identities_revealed'):
            return {'status': 'revealed'}
        guesser = db.get_drafter_by_user(draft_id, user_id)
        target = db.get_drafter_by_user(draft_id, target_id)
        if not guesser or not target:
            return {'status': 'invalid', 'message': 'Both you and the suspected GM must be in this anonymous draft.'}
        if user_id == target_id:
            return {'status': 'invalid', 'message': 'You cannot suspect yourself.'}
        matches = [t for t in teams(draft) if logo_matches(emoji, t['logo'])]
        if len(matches) != 1:
            return {'status': 'invalid', 'message': 'Use one uniquely assigned team emoji from this draft.'}
        team = matches[0]
        if guesser['id'] in team['member_ids'] or any(
                guesser['id'] in t['member_ids'] and target['id'] in t['member_ids'] for t in teams(draft)):
            return {'status': 'invalid', 'message': 'You cannot suspect your own team or a teammate.'}
        correct = target['id'] in team['member_ids']
        phase = 'post_draft' if after_final_round(ctx) else 'draft'
        result = db.record_suspect(draft_id, message_id, user_id, target_id, team['logo'], correct,
                                   phase, draft['state'], CONSUME_WRONG_ATTEMPTS, ALLOW_REPEAT_CATCHES)
        result['logo'] = team['logo']
        if bot is not None and result['status'] in ('correct', 'wrong'):
            if correct:
                penalty = '5 wins' if result['penalty_kind'] == 'wins' else '10% odds in your next matchup'
                notice = f"You were correctly suspected of being the GM of {team['logo']}. You lose {penalty}."
            else:
                notice = f"Someone suspected you of being the GM of {team['logo']}, but the guess was incorrect. No penalty applies."
            await ctx.dm(target_id, notice)
        return result
