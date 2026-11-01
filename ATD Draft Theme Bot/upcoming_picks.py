"""Private upcoming pick numbers from the actual Theme draft scheduler."""
import discord
import db
import themes


def schedule(user_id):
    memberships = db.get_drafters_for_user(user_id, ('setup', 'active', 'paused', 'complete', 'cancelled'))
    if not memberships:
        raise ValueError('You are not registered in a Theme draft.')
    drafter = max(memberships, key=lambda d: d['draft_id'])
    draft = db.get_draft(drafter['draft_id'])
    if draft['status'] == 'cancelled':
        raise ValueError('Your latest draft was cancelled. Ask the commissioner which draft to use.')
    state = draft['state']
    rows = []
    if draft['theme'] in ('anon_draft', 'anon_budget_draft'):
        teams = state.get('teams', [])
        indices = [i for i, t in enumerate(teams) if drafter['id'] in t['member_ids']]
        if len(indices) != 1:
            raise ValueError('Your team has not been assigned yet. Ask the commissioner to run `!setteams`.')
        index = indices[0]
        team = teams[index]
        logo = team['logo']
        rounds = draft['settings'].get('rounds', 10)
        count = len(teams)
        completed = {number for mid in team['member_ids'] for number in db.get_pick_numbers(draft['id'], mid)}
        entries = {p['pick_number']: p for p in team.get('open_picks', [])}
        theme = themes.get_theme(draft['theme'])
        own = {number for number in range(1, count*rounds+1) if theme._team_index_for_pick(number, count, rounds) == index}
        own.update(entries)
        live = state.get('live_pick_number')
        finished = draft['status'] == 'complete' or state.get('scheduled_turns_finished')
        for number in sorted(own):
            if number in completed:
                continue
            entry = entries.get(number, {})
            if entry.get('skipped'):
                note = 'Makeup owed'
            elif finished or live is not None and number < live:
                continue
            elif number == live:
                if state.get('flux_wait'):
                    note = 'Waiting for Flux'
                elif entry:
                    note = 'On the clock' if draft['status'] == 'active' else 'Paused'
                else:
                    continue  # A manual override opened this slot for another team.
            else:
                note = ''
            rows.append(dict(round=(number-1)//count+1, pick=number, note=note))
    elif draft['theme'] == 'partner_draft':
        pairs = state.get('pairs', [])
        indices = [i for i, p in enumerate(pairs) if drafter['id'] in (p['a'], p['b'])]
        if len(indices) != 1 or not state.get('turns'):
            raise ValueError('Partnerships and pick order have not been created yet. Ask the commissioner to start the draft.')
        index = indices[0]
        pair = pairs[index]
        logo = pair['logo']
        pointer = state.get('turn_index', 0)
        for offset, turn in enumerate(state['turns']):
            round_index = turn['round']
            if turn['pair'] != index or round_index in pair.get('completed_rounds', []):
                continue
            if round_index in pair.get('owed_rounds', []):
                note = 'Makeup owed'
            elif offset < pointer or draft['status'] == 'complete':
                continue
            elif offset == pointer:
                note = 'Waiting for Flux' if state.get('flux_wait') else 'On the clock' if draft['status'] == 'active' else 'Paused'
            else:
                note = ''
            rows.append(dict(round=round_index+1, pick=offset+1, note=note))
    else:
        raise ValueError('`!nextpick` currently supports Anonymous Draft, Anonymous Budget and Partner Draft.')
    return draft, logo, rows


def embed(user_id):
    draft, logo, rows = schedule(user_id)
    lines = [f'**Your team:** {logo}', '']
    for row in rows[:30]:
        suffix = f" \u00b7 **{row['note']}**" if row['note'] else ''
        lines.append(f"Round {row['round']} \u2014 Pick **#{row['pick']}**{suffix}")
    if not rows:
        lines.append('Your team has no remaining picks or owed makeup picks.')
    if draft['state'].get('flux_wait'):
        lines += ['', 'The draft is waiting for the commissioner to complete Flux.']
    result = discord.Embed(title='Your Upcoming Picks', description='\n'.join(lines), color=0x3498DB)
    result.set_footer(text=(f'Showing 30 of {len(rows)} remaining picks. ' if len(rows)>30 else '')+
        ('Draft paused. ' if draft['status']=='paused' else '')+"Follows this draft's actual pick order.")
    return result
