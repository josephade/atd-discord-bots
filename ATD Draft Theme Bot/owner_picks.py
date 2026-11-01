"""Private owner submissions, using the anonymous draft's normal finalizer."""
import db
import themes
from config import OWNER_ID, OWNER_REPORT_CHANNEL_ID
from suspects import logo_matches
from themes.base import DraftContext, draft_lock


async def submit(bot, draft_id, actor_id, channel_id, guild_id, team_ref, text, message_id):
    if actor_id != OWNER_ID:
        return 'Only the bot owner can use `!anonpick`.'
    if not guild_id or not OWNER_REPORT_CHANNEL_ID or channel_id != OWNER_REPORT_CHANNEL_ID:
        return f'Use `!anonpick` only in <#{OWNER_REPORT_CHANNEL_ID}>.'
    async with draft_lock(draft_id):
        ctx = DraftContext(bot, draft_id)
        draft = ctx.draft
        if not draft or draft['guild_id'] != guild_id:
            return 'Draft not found in this server.'
        if draft['theme'] not in ('anon_draft', 'anon_budget_draft'):
            return 'Use this command for Anonymous Draft or Anonymous Budget Draft.'
        if draft['status'] != 'active':
            return 'This draft must be active before you can submit a pick.'
        state = ctx.state
        teams = state.get('teams', [])
        if team_ref.isdigit():
            indices = [int(team_ref)-1] if 1 <= int(team_ref) <= len(teams) else []
        else:
            indices = [i for i, team in enumerate(teams) if logo_matches(team_ref, team['logo'])]
        if len(indices) != 1:
            return 'Use a team number from !setteams or one uniquely assigned team emoji.'
        index = indices[0]
        team = teams[index]
        entry = next(iter(team.get('open_picks', [])), None)
        if entry is None or (not entry.get('skipped') and entry['pick_number'] != state.get('live_pick_number')):
            return 'That team has no owed makeup pick and is not on the clock. No pick was accepted.'
        if state.get('flux_in_progress') or (state.get('flux_wait') and not entry.get('skipped')):
            return 'Wait for Flux to finish before submitting the next-round pick.'
        theme = themes.get_theme(draft['theme'])
        parsed = theme._parse_submission(ctx, text)
        if parsed is None:
            return 'Use `!anonpick <draft ID> <team emoji/number> <player> [year] <price>`. Year is optional; a price is required for budget drafts.'
        player, year, price = parsed
        taken = theme._already_taken(ctx, player)
        if taken:
            return f'{player} has already been drafted by {taken}. No pick was accepted.'
        if draft['theme'] == 'anon_budget_draft':
            error = await theme.validate_round_price(ctx, player, price, entry=entry)
            if error:
                return error
        error = theme._check_budget(ctx, team, price)
        if error:
            return error
        number = entry['pick_number']
        # Owner submissions are final decisions, bypassing teammate approval.
        # This still spends once, clears lists, replies to owed skips and
        # advances the normal clock through the existing finalizer.
        await theme._finalize_pick(ctx, index, entry, player, year, price)
        fresh = ctx.state
        if any(p['pick_number'] == number for p in fresh['teams'][index]['open_picks']):
            return 'The pick failed final validation. Check the live price and budget, then try again.'
        audit = fresh.setdefault('owner_pick_audit', [])
        audit.append(dict(message_id=message_id, owner_id=actor_id, pick_number=number,
                          team_index=index, player=player, year=year, price=price))
        fresh['owner_pick_audit'] = audit[-1000:]
        ctx.save_state(fresh)
        return f"Recorded pick #{number} for {team['logo']}: **{player}**."
