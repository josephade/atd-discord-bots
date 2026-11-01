"""Private ranked lists for Anonymous Budget; Theme Bot owns all decisions."""
import math
import re
import asyncio
import json

import db
import themes
import sheets
from flux_rules import parse_prices, qualify, threshold, failure_message, failure_reports, key as player_key
from themes.base import DraftContext, draft_lock


def participant(draft_id, user_id, guild_id):
    draft = db.get_draft(draft_id)
    if not draft or draft['guild_id'] != guild_id or draft['theme'] != 'anon_budget_draft':
        raise ValueError('That is not an Anonymous Budget draft in this server.')
    drafter = db.get_drafter_by_user(draft_id, user_id)
    if not drafter or drafter['state'].get('team_index') is None:
        raise ValueError('You are not assigned to a team in that draft yet.')
    return draft, drafter


def view(draft, drafter):
    queue = drafter['state'].get('draftlist_queue', {})
    ready, error = flux_window(draft, drafter)
    return dict(version=queue.get('version', ''), picks=queue.get('picks', []),
                enabled=queue.get('enabled', True), status=draft['status'],
                revision=queue.get('revision', 0), can_fluxpick=ready, fluxpick_error=error,
                unavailable=queue.get('unavailable', []))


def flux_window(draft, drafter):
    state = draft['state']
    teams = state.get('teams', [])
    idx = drafter['state'].get('team_index')
    if idx is None or not teams or not state.get('live_pick_number'):
        return False, 'There is no upcoming Flux to queue a pick for.'
    round_number = (state['flux_wait']['round']-1 if state.get('flux_wait') else
                    (state['live_pick_number']-1)//len(teams)+1)
    if round_number >= draft['settings'].get('rounds', 10):
        return False, 'There is no next draft round to queue a Flux pick for.'
    if state.get('flux_in_progress'):
        return False, 'Flux has already started. Submit a normal pick when play resumes.'
    numbers = db.get_pick_numbers(draft['id'], teams[idx]['member_ids'][0])
    if (not any((round_number-1)*len(teams) < number <= round_number*len(teams) for number in numbers)
            or teams[idx].get('open_picks')):
        return False, 'Finish your team’s current-round pick and any owed makeup before setting a Flux pick for the next round.'
    return True, ''


def members(draft_id, owner_id, guild_id):
    from config import OWNER_ID
    if owner_id != OWNER_ID:
        raise ValueError('Only the bot owner can change the draft mode for everyone.')
    draft = db.get_draft(draft_id)
    if not draft or draft['guild_id'] != guild_id or draft['theme'] != 'anon_budget_draft':
        raise ValueError('That is not an Anonymous Budget draft in this server.')
    ids = list(dict.fromkeys(mid for team in draft['state'].get('teams', []) for mid in team['member_ids']))
    if not ids:
        raise ValueError('Start the draft and assign teams before enabling Anonymous Budget lists.')
    drafters = [db.get_drafter(mid) for mid in ids]
    return draft, drafters


async def roster(draft_id, owner_id, guild_id, action=None, price_source=None):
    async with draft_lock(draft_id):
        draft, drafters = members(draft_id, owner_id, guild_id)
        if action not in (None, 'enable', 'disable', 'prices'):
            raise ValueError('Use enable or disable for the owner draft mode.')
        if action not in ('disable', 'prices') and draft['status'] not in ('active', 'paused'):
            raise ValueError('That draft is not active or paused.')
        if price_source is not None:
            if (not isinstance(price_source, dict) or not re.fullmatch(r'[A-Za-z0-9_-]{10,}', str(price_source.get('sheet', '')))
                    or not isinstance(price_source.get('tab'), str) or not 1 <= len(price_source['tab']) <= 100):
                raise ValueError('A valid Flux price sheet and tab are required.')
            db.update_draft_state(draft_id, {'flux_price_source': price_source})
        if action in ('enable', 'disable'):
            previously_disabled = draft['state'].get('draftlist_enabled') is False
            db.update_draft_state(draft_id, {'draftlist_enabled': action == 'enable'})
            for drafter in drafters:
                queue = drafter['state'].get('draftlist_queue', {})
                if action == 'disable':
                    queue.update(picks=[], enabled=False, revision=queue.get('revision', 0)+1)
                elif previously_disabled:
                    queue.update(enabled=True, revision=queue.get('revision', 0)+1)
                if queue:
                    db.update_drafter_state(drafter['id'], {'draftlist_queue': queue})
            drafters = [db.get_drafter(d['id']) for d in drafters]
        return {'members': [dict(user_id=str(d['user_id']), **view(draft, d)) for d in drafters]}


def validate(picks):
    if not isinstance(picks, list) or len(picks) > 100:
        raise ValueError('Use a ranked list of at most 100 players.')
    clean, seen = [], set()
    for pick in picks:
        if not isinstance(pick, dict):
            raise ValueError('Each list entry must contain a player and price.')
        player, year = str(pick.get('player', '')).strip(), str(pick.get('year') or '').strip()
        if not player or len(player) > 100 or not any(c.isalpha() for c in player) or any(c in player for c in '\r\n<>@'):
            raise ValueError('Each entry needs a valid player name.')
        if year and not re.fullmatch(r"'?\d{2}(?:\d{2})?(?:-\d{2}(?:\d{2})?)?", year):
            raise ValueError(f'{player}: use an optional year such as 23 or 2022-23.')
        try:
            price = float(pick['price'])
        except (KeyError, TypeError, ValueError):
            raise ValueError(f'{player}: a price is required, for example Michael Jordan $44.') from None
        if not math.isfinite(price) or price < 0:
            raise ValueError(f'{player}: price must be a finite, non-negative number.')
        if pick.get('lock'):
            raise ValueError('Lock picks are not supported in Anonymous Budget.')
        key = player.casefold()
        if key not in seen:
            # Draft List expands '23 to 2022-23; public picks use the ending year.
            normalized_year = year.lstrip("'").rsplit('-', 1)[-1][-2:] if year else ''
            item = dict(player=player, year=normalized_year, price=price)
            if 'flux_drop' in pick:
                source = pick.get('flux_source', {})
                if (not isinstance(source, dict) or not re.fullmatch(r'[A-Za-z0-9_-]{10,}', str(source.get('sheet', '')))
                        or not isinstance(source.get('tab'), str) or not 1 <= len(source['tab']) <= 100):
                    raise ValueError('A verified Flux sheet and tab are required for conditional picks.')
                item.update(flux_drop=threshold(pick['flux_drop']), flux_source={'sheet': source['sheet'], 'tab': source['tab']})
                start, target = pick.get('flux_from_round'), pick.get('flux_target_round')
                if not isinstance(start, int) or isinstance(start, bool) or start < 1 or target != start+1:
                    raise ValueError('Flux picks must be submitted before the next Flux. Submit a fresh rule after your current pick.')
                item.update(flux_from_round=start, flux_target_round=target)
            clean.append(item)
            seen.add(key)
    return clean


async def update(bot, draft_id, user_id, guild_id, payload):
    async with draft_lock(draft_id):
        draft, drafter = participant(draft_id, user_id, guild_id)
        if draft['state'].get('draftlist_enabled') is False and (payload.get('picks') or payload.get('enabled', True)):
            raise ValueError('The owner has disabled Anonymous Budget lists for this draft.')
        if draft['status'] not in ('active', 'paused') and (payload.get('picks') or payload.get('enabled', True)):
            raise ValueError('This draft is not active or paused. Lists cannot be submitted now.')
        old = drafter['state'].get('draftlist_queue', {})
        version = payload.get('version')
        if not isinstance(version, str) or not 1 <= len(version) <= 64:
            raise ValueError('A list update ID is required.')
        # Replaying a successful upload after a timeout never restores a used list.
        if version == old.get('version'):
            return view(draft, drafter)
        replacing = payload.get('replace_flux') is True
        submission_id = payload.get('submission_id', 0)
        if not isinstance(submission_id, int) or isinstance(submission_id, bool) or submission_id < 0:
            raise ValueError('Invalid list submission ID.')
        if replacing and submission_id and submission_id < old.get('submission_id', 0):
            return dict(view(draft, drafter), superseded=True)
        if not replacing and payload.get('expected_revision', 0) != old.get('revision', 0):
            raise ValueError('Your list changed while it was being edited. Refresh it and try again.')
        if not isinstance(payload.get('enabled', True), bool):
            raise ValueError('Enabled must be true or false.')
        picks = validate(payload.get('picks', []))
        state = draft['state']
        team = state['teams'][drafter['state']['team_index']]
        if replacing:
            ready, error = flux_window(draft, drafter)
            if not ready:
                raise ValueError(error)
        unavailable, available = [], []
        for pick in picks:
            if 'flux_drop' not in pick:
                available.append(pick)
                continue
            if themes.get_theme('anon_budget_draft')._already_taken(DraftContext(bot, draft_id), pick['player']):
                unavailable.append(pick['player'])
                continue
            prior = next((p for p in old.get('picks', []) if p.get('player', '').casefold() == pick['player'].casefold()
                          and p.get('flux_target_round') == pick['flux_target_round'] and p.get('flux_drop') == pick['flux_drop']
                          and p.get('flux_source') == pick['flux_source']), None)
            if prior and not replacing:
                available.append(pick)
                continue  # pause/resume and edits to other entries preserve an existing rule.
            ready, error = flux_window(draft, drafter)
            if not ready:
                raise ValueError(error)
            current_round = (state['flux_wait']['round']-1 if state.get('flux_wait') else
                             (state['live_pick_number']-1)//len(state['teams'])+1)
            if pick['flux_from_round'] != current_round:
                raise ValueError('Submit fresh rules for the next Flux after your current-round pick.')
            available.append(pick)
        queue = dict(version=version, picks=available, enabled=payload.get('enabled', True),
                     revision=old.get('revision', 0)+1, unavailable=unavailable,
                     submission_id=submission_id if replacing else old.get('submission_id', 0))
        db.update_drafter_state(drafter['id'], {'draftlist_queue': queue})
        ctx = DraftContext(bot, draft_id)
        state = ctx.state
        idx = drafter['state']['team_index']
        team = state['teams'][idx]
        entry = next((p for p in team.get('open_picks', []) if p['pick_number'] == state.get('live_pick_number') and not p.get('skipped')), None)
        if draft['status'] == 'active' and not state.get('flux_wait') and entry and not entry.get('proposal'):
            await consume(ctx, state, idx, entry)
        return view(ctx.draft, db.get_drafter(drafter['id']))


async def consume(ctx, state, team_idx, entry):
    team = state['teams'][team_idx]
    if ctx.draft['status'] != 'active' or state.get('draftlist_enabled') is False or state.get('flux_in_progress') or state.get('flux_wait') or entry.get('skipped') or entry.get('proposal'):
        return False
    theme = themes.get_theme('anon_budget_draft')
    books = {}
    for mid in team['member_ids']:
        drafter = db.get_drafter(mid)
        queue = drafter['state'].get('draftlist_queue', {})
        if not queue.get('enabled', True):
            continue
        picks = queue.get('picks', [])
        for original in list(picks):
            pick = original
            if 'flux_drop' in pick:
                if 'flux_target_round' not in pick:
                    continue
                token = json.dumps(pick['flux_source'], sort_keys=True)
                if token not in books:
                    try:
                        rows = await asyncio.to_thread(sheets.load_flux_rule_rows, pick['flux_source'])
                        books[token] = parse_prices(rows)
                    except Exception:
                        books[token] = {}
                pick = qualify(pick, books[token])
                if pick is None:
                    continue  # Keep the rule while this turn waits for Flux.
            player, year, price = pick['player'], pick['year'], pick['price']
            taken = theme._already_taken(ctx, player)
            error = f'{player} has already been drafted.' if taken else await theme.validate_round_price(ctx, player, price)
            if not error:
                error = theme._check_budget(ctx, team, price)
            if error and 'flux_drop' in original and not taken:
                continue
            picks.remove(original)
            queue.update(picks=picks, revision=queue.get('revision', 0)+1)
            db.update_drafter_state(mid, {'draftlist_queue': queue})
            if error:
                await ctx.dm(drafter['user_id'], f'Your Draft List skipped {player}: {error}')
                continue
            if len(team['member_ids']) == 1:
                await theme._finalize_pick(ctx, team_idx, entry, player, year, price)
            else:
                await theme._propose(ctx, state, team_idx, entry, drafter, player, year, price)
            return True
    return False


async def settle_flux(ctx, completed_round=None, legacy_only=False):
    books = {}
    for drafter in ctx.drafters():
        queue = drafter['state'].get('draftlist_queue', {})
        remaining, notices = [], []
        for pick in queue.get('picks', []):
            if 'flux_drop' not in pick or (legacy_only and 'flux_target_round' in pick):
                remaining.append(pick)
                continue
            target = pick.get('flux_target_round')
            if target is not None and completed_round is not None and target > completed_round:
                remaining.append(pick)
                continue
            token = json.dumps(pick['flux_source'], sort_keys=True)
            if token not in books:
                try:
                    rows = await asyncio.to_thread(sheets.load_flux_rule_rows, pick['flux_source'])
                    books[token] = parse_prices(rows)
                except Exception:
                    books[token] = {}
            eligible = qualify(pick, books[token])
            if eligible is not None and target is not None:
                remaining.append(pick)
                continue
            notices.append(failure_message(pick, books[token]) if eligible is None else
                           f"Your old Flux rule for {pick['player']} was cleared because it was not tied to an upcoming Flux. Finish your current pick before submitting a fresh rule.")
        if notices:
            queue.update(picks=remaining, revision=queue.get('revision', 0)+1)
            db.update_drafter_state(drafter['id'], {'draftlist_queue': queue})
            for notice in failure_reports(notices):
                await ctx.dm(drafter['user_id'], notice)


async def observe_flux(bot, message, run=False):
    from config import FLUX_BOT_ID
    if not message.author.bot or message.author.id != FLUX_BOT_ID:
        return
    text = message.content.replace('*', '').strip()
    completed = bool(re.fullmatch(r'✅\s*Round\s+\d+\s+Flux is done', text, re.I))
    started = 'flux is in progress' in text.casefold()
    if not completed and not started:
        return
    for draft in db.list_drafts():
        if (draft['theme'] != 'anon_budget_draft' or draft['status'] not in ('active', 'paused')
                or draft['state'].get('timer_channel_id') != message.channel.id):
            continue
        async with draft_lock(draft['id']):
            ctx = DraftContext(bot, draft['id'])
            state = ctx.state
            if not run:
                state['flux_in_progress'] = not completed
                ctx.save_state(state)
                if completed:
                    round_number = int(re.search(r'Round\s+(\d+)', text, re.I)[1])
                    await settle_flux(ctx, round_number)
                continue
            if not completed or message.id <= state.get('last_list_flux_message_id', 0):
                continue
            state['last_list_flux_message_id'] = message.id
            ctx.save_state(state)
            for idx, team in enumerate(state.get('teams', [])):
                entry = next((p for p in team.get('open_picks', []) if p['pick_number'] == state.get('live_pick_number')), None)
                if entry:
                    await consume(ctx, state, idx, entry)
                    break


async def recover_flux(bot):
    from config import FLUX_BOT_ID
    channels = {draft['state'].get('timer_channel_id') for draft in db.list_drafts()
                if draft['theme'] == 'anon_budget_draft' and draft['state'].get('flux_in_progress')}
    for channel_id in channels:
        channel = bot.get_channel(channel_id)
        if channel is None:
            continue
        async for message in channel.history(limit=100):
            if not message.author.bot or message.author.id != FLUX_BOT_ID:
                continue
            text = message.content.replace('*', '').strip()
            if 'flux is in progress' in text.casefold() or re.fullmatch(r'✅\s*Round\s+\d+\s+Flux is done', text, re.I):
                await observe_flux(bot, message)
                break
    for draft in db.list_drafts():
        if draft['theme'] == 'anon_budget_draft' and draft['status'] in ('active', 'paused'):
            async with draft_lock(draft['id']):
                await prune_taken(DraftContext(bot, draft['id']))
                await settle_flux(DraftContext(bot, draft['id']), legacy_only=True)


def clear_team(team):
    for mid in team['member_ids']:
        drafter = db.get_drafter(mid)
        queue = drafter['state'].get('draftlist_queue')
        if queue:
            queue.update(picks=[], revision=queue.get('revision', 0)+1)
            db.update_drafter_state(mid, {'draftlist_queue': queue})


async def prune_taken(ctx, taken=None, exclude=()):
    """Remove drafted options immediately, including paused lists and restarts."""
    state = ctx.state
    if taken is None:
        taken = {}
        for mid, names in db.get_all_picks(ctx.draft_id).items():
            drafter = db.get_drafter(mid)
            index = drafter['state'].get('team_index') if drafter else None
            logo = state['teams'][index]['logo'] if index is not None else 'another team'
            for name in names:
                taken[player_key(name)] = logo
    notices = []
    for drafter in ctx.drafters():
        if drafter['id'] in exclude:
            continue
        queue = drafter['state'].get('draftlist_queue')
        if not queue:
            continue
        removed = [p for p in queue.get('picks', []) if player_key(p['player']) in taken]
        if not removed:
            continue
        queue['picks'] = [p for p in queue['picks'] if player_key(p['player']) not in taken]
        queue['revision'] = queue.get('revision', 0)+1
        db.update_drafter_state(drafter['id'], {'draftlist_queue': queue})
        lines = [f"**{p['player']}** was drafted by {taken[player_key(p['player'])]} and removed from your list." for p in removed]
        chunk = '⚠️ **Draft List update**\n'
        for line in lines:
            if len(chunk)+len(line) > 1700:
                notices.append((drafter['user_id'], chunk))
                chunk = ''
            chunk += line+'\n'
        notices.append((drafter['user_id'], chunk+f"**{len(queue['picks'])} options remaining.**"))
    await asyncio.gather(*(ctx.dm(uid, message) for uid, message in notices))
