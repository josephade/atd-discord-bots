"""Owner-only private membership edits after !setteams."""
import re
import discord
import db
from config import OWNER_ID, OWNER_REPORT_CHANNEL_ID
from suspects import logo_matches
from themes.base import DraftContext, draft_lock


def user_id(text):
    match = re.fullmatch(r'<@!?(\d+)>|(\d+)', text)
    if not match:
        raise ValueError('Mention the GM or provide their Discord user ID.')
    return int(match.group(1) or match.group(2))


async def execute(ctx, bot, text, replace=False):
    if ctx.author.id != OWNER_ID:
        return
    if not ctx.guild or ctx.channel.id != OWNER_REPORT_CHANNEL_ID:
        await ctx.send(f'Use this command only in <#{OWNER_REPORT_CHANNEL_ID}>.')
        return
    if ctx.channel.permissions_for(ctx.guild.default_role).view_channel:
        await ctx.send('Hide this channel from @everyone before editing secret GM assignments.')
        return
    command = '!anonreplacegm <draft ID> @oldGM @newGM' if replace else '!anonaddgm <draft ID> <team emoji/lotto number> @newGM'
    parts = text.split()
    try:
        if len(parts) != 3 or not parts[0].isdigit():
            raise ValueError('Usage: `'+command+'`.')
        draft_id = int(parts[0])
        new_user = user_id(parts[2])
        old_user = user_id(parts[1]) if replace else None
        member = ctx.guild.get_member(new_user)
        if member is None:
            try:
                member = await ctx.guild.fetch_member(new_user)
            except discord.HTTPException:
                raise ValueError('The new GM must be a member of this server. Check their mention or user ID.')
        async with draft_lock(draft_id):
            draft = db.get_draft(draft_id)
            if not draft or draft['guild_id'] != ctx.guild.id or draft['theme'] not in ('anon_draft', 'anon_budget_draft'):
                raise ValueError('Choose an Anonymous Draft or Anonymous Budget draft in this server.')
            teams = draft['state'].get('teams', [])
            if not teams:
                raise ValueError('Run `!setteams` first.')
            if replace:
                old = db.get_drafter_by_user(draft_id, old_user)
                matches = [i for i, team in enumerate(teams) if old and old['id'] in team['member_ids']]
            elif parts[1].isdigit():
                matches = [int(parts[1])-1] if 1 <= int(parts[1]) <= len(teams) else []
            else:
                matches = [i for i, team in enumerate(teams) if logo_matches(parts[1], team['logo'])]
            if len(matches) != 1:
                raise ValueError('Choose one existing team or GM from this draft.')
            index = matches[0]
            result = db.edit_anonymous_member(draft_id, draft['state'], index, new_user, member.display_name, ctx.author.id, old_user)
            if result['cleared_proposal']:
                from themes.anon_draft import _proposal_timer
                _proposal_timer.cancel((draft_id, index))
        draft_ctx = DraftContext(bot, draft_id)
        team = draft_ctx.state['teams'][index]
        notice = f"You have been assigned to **{draft['name']}**.\nYour team: {team['logo']}\nLotto number: **{index+1}**.\n"
        if draft['theme'] == 'anon_budget_draft':
            total = draft['settings'].get('budget', 100)
            notice += f"Budget remaining: **${total-team.get('budget_spent', 0):g} of ${total:g}**.\n"
        notice += 'The existing roster and timer stay with the team. DM `!myteam` to view your roster. Keep your team secret.'
        if result['cleared_proposal']:
            notice += '\nPending teammate approval was cleared. Submit a fresh pick if needed.'
        delivered = await draft_ctx.dm(new_user, notice)
        if draft['status'] in ('active', 'paused'):
            from timer_bridge import snapshot
            from clock_card import clock_embed
            await draft_ctx.dm(new_user, embed=clock_embed(snapshot(draft_ctx.draft)))
        action = 'Replaced <@'+str(old_user)+'> with' if replace else 'Added co-GM'
        reply = f"{action} <@{new_user}> on {team['logo']}. Roster, budget, timer and suspect limits preserved."
        if result['cleared_proposal']:
            reply += ' Pending teammate approval cleared.'
        if not delivered:
            reply += ' Their assignment DM could not be delivered; ask them to enable server DMs.'
        if draft['state'].get('draftlist_enabled') is True:
            reply += f' Run `!draftmode anon {draft_id}` in Draft List Bot to sync the updated GM roster.'
        await ctx.send(reply, allowed_mentions=discord.AllowedMentions.none())
    except ValueError as exc:
        await ctx.send(str(exc), allowed_mentions=discord.AllowedMentions.none())
