"""Anonymous GM guesses, bounded attempts, and a persisted penalty ledger."""
import re
import json

import db
import themes
from emoji_map import EMOJI_TEAM_MAP
from themes.base import DraftContext, draft_lock

ANON_THEMES = frozenset(('anon_draft', 'anon_budget_draft', 'partner_draft'))
ALLOWED_CHANNEL_IDS = frozenset((1566394824288894976, 1533242104304631969))
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


def guessers_with_attempts(draft):
    """Return individual GMs only; never expose team or shared-account details."""
    if draft['theme'] not in ANON_THEMES or draft['state'].get('identities_revealed'):
        return []
    members = {d['id']: d for d in db.list_drafters(draft['id'])}
    eligible = {}
    for team in teams(draft):
        assigned = [members[mid] for mid in team['member_ids'] if mid in members]
        if db.suspect_remaining(draft['id'], [d['user_id'] for d in assigned]) > 0:
            for member in assigned:
                eligible[member['user_id']] = {'user_id': member['user_id'],
                                              'display_name': member['display_name']}
    return list(eligible.values())


def win_deduction_summary(draft):
    """One combined win deduction per current team, with no team logos."""
    ownership = {}
    for team in teams(draft):
        members = tuple(sorted(db.get_drafter(mid)['user_id'] for mid in team['member_ids']))
        for user_id in db.anonymous_ledger_users(draft['id'], members):
            ownership[user_id] = members
    totals = {}

    def bucket(users):
        members = next((ownership[uid] for uid in users if uid in ownership), tuple(sorted(users)))
        return totals.setdefault(members, dict(member_ids=members, caught_wins=0,
                                               market_wins=0, bought_guesses=0, total_wins=0))

    for attempt in db.suspect_attempts(draft['id']):
        if attempt['correct'] and attempt['penalty_kind'] == 'wins' and attempt['penalty_amount'] > 0:
            bucket([attempt['target_id']])['caught_wins'] += attempt['penalty_amount']
    for purchase in db.suspect_purchases(draft['id']):
        if purchase['status'] == 'confirmed' and purchase['cost'] > 0:
            entry = bucket(json.loads(purchase['team_users']) or [purchase['buyer_id']])
            entry['market_wins'] += purchase['cost']
            entry['bought_guesses'] += purchase['quantity']
    for entry in totals.values():
        entry['total_wins'] = entry['caught_wins'] + entry['market_wins']
    return sorted(totals.values(), key=lambda item: (-item['total_wins'], item['member_ids']))


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


def playoff_logos(draft):
    return draft['state'].get('suspect_seeding', {}).get('playoff_logos', [])


async def release_seeding(bot, draft_id, guild_id, owner_id, entries):
    """Explicit one-way phase transition; guesses and release share the draft lock."""
    async with draft_lock(draft_id):
        ctx = DraftContext(bot, draft_id)
        draft = ctx.draft
        if not draft or draft['guild_id'] != guild_id or draft['theme'] not in ANON_THEMES or draft['status'] not in ('active','paused','complete'):
            raise ValueError('Choose an anonymous draft in this server.')
        if draft['state'].get('identities_revealed'):
            raise ValueError('GM identities have already been revealed; suspect is closed.')
        if not after_final_round(ctx):
            raise ValueError('Finish the scheduled draft rounds before releasing seeding.')
        assigned = teams(draft)
        selections = [item.strip() for item in re.split(r'[,\n]+', entries) if item.strip()]
        selected = []
        for item in selections:
            matches = ([assigned[int(item)-1]] if item.isdigit() and 1 <= int(item) <= len(assigned) else
                       [team for team in assigned if logo_matches(item,team['logo'])])
            if len(matches) != 1:
                raise ValueError(f'Unknown or ambiguous team: {item}. Use team emojis or original lotto numbers, one per line.')
            selected.append(matches[0]['logo'])
        if len(selected) != 16 or len(set(selected)) != 16:
            raise ValueError('Supply exactly 16 distinct playoff teams, one emoji or original lotto number per line.')
        existing = draft['state'].get('suspect_seeding')
        if existing:
            if set(existing['playoff_logos']) == set(selected):
                return draft
            raise ValueError('Seeding has already been released. The saved playoff pool cannot be replaced with this command.')
        from datetime import datetime, timezone
        state = dict(draft['state'])
        state['suspect_seeding'] = dict(playoff_logos=selected,released_by=owner_id,
                                        released_at=datetime.now(timezone.utc).isoformat())
        db.set_draft_state(draft_id,state)
        return db.get_draft(draft_id)


def phase_summary(draft):
    seeding = draft['state'].get('suspect_seeding')
    if seeding:
        return ('**Seeding released - playoff phase**\n'
                'Correct guess: **10 percentage points off next playoff matchup odds**.\n'
                '**Eligible teams (16):** '+ ' '.join(seeding['playoff_logos']))
    return ('**Before seeding - drafting/voting phase**\n'
            'Correct guess: **5 wins deducted before final seeding**. All teams remain in the pool.\n'
            'The end of Round 10 does not change this phase.')


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
        if not guesser:
            return {'status': 'invalid', 'message': 'You must be registered in this anonymous draft to guess.'}
        own_teams = [t for t in teams(draft) if guesser['id'] in t['member_ids']]
        if len(own_teams) != 1:
            return {'status': 'invalid', 'message': 'Your team assignment is unclear. Ask the commissioner to check it.'}
        attempt_users = [db.get_drafter(mid)['user_id'] for mid in own_teams[0]['member_ids']]
        attempt_users = db.anonymous_ledger_users(draft_id, attempt_users)
        target_teams = [t for t in teams(draft) if target and target['id'] in t['member_ids']]
        if target and len(target_teams) != 1:
            return {'status': 'invalid', 'message': 'The targeted GM\u2019s team assignment is unclear. Ask the commissioner to check it.'}
        caught_users = [db.get_drafter(mid)['user_id'] for mid in target_teams[0]['member_ids']] if target_teams else [target_id]
        caught_users = db.anonymous_ledger_users(draft_id, caught_users)
        matches = [t for t in teams(draft) if logo_matches(emoji, t['logo'])]
        if len(matches) != 1:
            return {'status': 'invalid', 'message': 'Use one uniquely assigned team emoji from this draft.'}
        team = matches[0]
        # Check only the publicly guessed logo; checking the GM's real team would leak ownership.
        if draft['state'].get('suspect_seeding') and team['logo'] not in playoff_logos(draft):
            return {'status': 'ineligible'}
        # Self/own-team/teammate guesses spend the shared life as wrong guesses.
        # Rejecting them for free would permit unlimited apparent guesses and
        # a distinguishable response could expose the anonymous relationship.
        own_guess = (user_id == target_id or guesser['id'] in team['member_ids']
                     or target is not None and target['id'] in own_teams[0]['member_ids'])
        correct = target is not None and not own_guess and target['id'] in team['member_ids']
        phase = 'post_draft' if draft['state'].get('suspect_seeding') else 'draft'
        result = db.record_suspect(draft_id, message_id, user_id, target_id, team['logo'], correct,
                                   phase, draft['state'], CONSUME_WRONG_ATTEMPTS, ALLOW_REPEAT_CATCHES,
                                   attempt_user_ids=attempt_users, caught_user_ids=caught_users)
        result['logo'] = team['logo']
        if bot is not None and target is not None and target_id != user_id and result['status'] in ('correct', 'wrong'):
            if correct:
                penalty = '5 wins' if result['penalty_kind'] == 'wins' else '10 percentage points of odds in your next playoff matchup'
                notice = f"You were correctly suspected of being the GM of {team['logo']}. You lose {penalty}."
            else:
                notice = f"Someone suspected you of being the GM of {team['logo']}, but the guess was incorrect. No penalty applies."
            await ctx.dm(target_id, notice)
        return result
