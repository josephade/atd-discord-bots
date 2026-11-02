"""Private owner report of the largest round-to-round Flux price drops."""
import asyncio
import logging
from datetime import datetime, timezone
import discord
import db
import sheets
from config import OWNER_ID, OWNER_REPORT_CHANNEL_ID
from flux_rules import parse_prices, key, find_player

PAGE_SIZE = 15


def current_round(draft):
    state = draft['state']
    if state.get('flux_in_progress'):
        raise ValueError('Flux is still in progress. Run `!steal` after it finishes.')
    teams = state.get('teams', [])
    if not teams:
        raise ValueError('Teams have not been assigned yet.')
    if state.get('flux_wait'):
        return state['flux_wait']['round']-1
    if state.get('live_pick_number'):
        return (state['live_pick_number']-1)//len(teams)+1
    if state.get('scheduled_turns_finished') or draft['status']=='complete':
        return draft['settings'].get('rounds', 10)
    raise ValueError('The draft has not started yet.')



def highlighted_names(metadata):
    """Recognize the draft board's blue taken fill and legacy blackouts."""
    names = []
    for sheet in metadata.get('sheets', []):
        for grid in sheet.get('data', []):
            for row in grid.get('rowData', []):
                for cell in row.get('values', []):
                    bg = cell.get('effectiveFormat', {}).get('backgroundColor')
                    if bg is None:
                        continue
                    # Sheets omits zero channels (black can be an empty dict).
                    rgb = tuple(bg.get(channel, 0) for channel in ('red', 'green', 'blue'))
                    blue = all(abs(value-target) < .04 for value,target in zip(rgb,(66/255,129/255,232/255)))
                    if (blue or max(rgb) < .2) and cell.get('formattedValue'):
                        names.append(cell['formattedValue'])
    return names

def rank_drops(rows, round_number, drafted_names=()):
    if round_number < 2:
        raise ValueError('A comparison is available after the first Flux, when Round 2 prices are published.')
    parse_prices(rows, round_number=round_number-1)  # Require the actual previous round; never bridge a gap.
    book = parse_prices(rows, round_number=round_number)
    drafted = set()
    for name in drafted_names:
        try: drafted.add(key(find_player(book, name)['player']))
        except ValueError: pass
    ranked, excluded = [], 0
    for record in book.values():
        if key(record['player']) in drafted:
            continue
        previous, current = record['previous'], record['current']
        if previous is None or current is None or record.get('ambiguous'):
            excluded += 1
            continue
        drop = previous-current
        if drop > 0:
            ranked.append(dict(player=record['player'], previous=previous, current=current, drop=drop,
                               drafted=key(record['player']) in drafted))
    ranked.sort(key=lambda p: (-p['drop'], p['current'], p['player'].casefold()))
    return ranked, excluded


def choose_draft(guild_id, draft_ref=''):
    if draft_ref:
        if not draft_ref.isdigit(): raise ValueError('Use `!steal` or `!steal <Theme draft ID>`.')
        draft = db.get_draft(int(draft_ref))
    else:
        matches = [d for d in db.list_drafts(guild_id=guild_id) if d['theme']=='anon_budget_draft' and d['status'] in ('active','paused')]
        if len(matches)!=1:
            raise ValueError('Use `!steal <Theme draft ID>` to choose one active Anonymous Budget draft.')
        draft = matches[0]
    if not draft or draft['guild_id']!=guild_id or draft['theme']!='anon_budget_draft' or draft['status'] not in ('active','paused','complete'):
        raise ValueError('Choose an active, paused or completed Anonymous Budget draft in this server.')
    return draft


async def load(guild_id, draft_ref=''):
    draft = choose_draft(guild_id, draft_ref)
    number = current_round(draft)
    if number < 2:
        raise ValueError('Run `!steal` after the first Flux. Round 1 has no previous round to compare.')
    source = draft['state'].get('flux_price_source')
    if not source: raise ValueError('No Flux price source is configured for this draft. Check `!fluxsource` in Draft List Bot.')
    rows, formatting = await asyncio.to_thread(sheets.load_flux_report_data, source)
    fresh = db.get_draft(draft['id'])
    if fresh['status'] not in ('active','paused','complete') or fresh['state'].get('flux_price_source')!=source or current_round(fresh)!=number:
        raise ValueError('The Flux round or source changed while loading. Run `!steal` again.')
    names = [name for picks in db.get_all_picks(draft['id']).values() for name in picks]
    ranked, excluded = rank_drops(rows, number, names + highlighted_names(formatting))
    return dict(draft_id=draft['id'], round=number, ranked=ranked, excluded=excluded, updated=datetime.now(timezone.utc))


def money(value):
    text = format(value, 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def report_embed(report, page=0):
    rows = report['ranked']; start=page*PAGE_SIZE
    lines=[f"**Round {report['round']-1} to Round {report['round']}** - available players, biggest drops first.", '']
    for index, row in enumerate(rows[start:start+PAGE_SIZE], start+1):
        status = ' - drafted' if row['drafted'] else ''
        lines.append(f"`{index}.` **{row['player']}** | ${money(row['previous'])} \u2192 ${money(row['current'])} | **-${money(row['drop'])}**{status}")
    if not rows: lines.append('No available players dropped in price between these rounds.')
    pages=max(1,(len(rows)+PAGE_SIZE-1)//PAGE_SIZE)
    footer=f"Page {page+1}/{pages} | {len(rows)} price drops | Taken players excluded"
    if report['excluded']: footer+=f" | {report['excluded']} missing/conflicting comparisons excluded"
    embed=discord.Embed(title='Flux Steals - Price Drops', description='\n'.join(lines), color=0x2ECC71, timestamp=report['updated'])
    embed.set_footer(text=footer)
    return embed


class ReportPages(discord.ui.View):
    def __init__(self, report):
        super().__init__(timeout=300)
        self.report, self.page=report,0
        self.update_buttons()

    def update_buttons(self):
        self.previous.disabled=self.page==0
        self.next.disabled=(self.page+1)*PAGE_SIZE>=len(self.report['ranked'])

    async def interaction_check(self, interaction):
        if interaction.user.id!=OWNER_ID or interaction.channel_id!=OWNER_REPORT_CHANNEL_ID:
            await interaction.response.send_message('This report is only for the owner in the private admin channel.',ephemeral=True)
            return False
        return True

    @discord.ui.button(label='Previous',style=discord.ButtonStyle.secondary)
    async def previous(self,interaction,button):
        self.page=max(0,self.page-1);self.update_buttons()
        await interaction.response.edit_message(embed=report_embed(self.report,self.page),view=self)

    @discord.ui.button(label='Next',style=discord.ButtonStyle.secondary)
    async def next(self,interaction,button):
        self.page=min(max(0,(len(self.report['ranked'])-1)//PAGE_SIZE),self.page+1);self.update_buttons()
        await interaction.response.edit_message(embed=report_embed(self.report,self.page),view=self)


async def show(ctx,draft_ref=''):
    if ctx.author.id!=OWNER_ID or not ctx.guild or ctx.channel.id!=OWNER_REPORT_CHANNEL_ID:
        return
    if ctx.channel.permissions_for(ctx.guild.default_role).view_channel:
        await ctx.send('Hide this channel from @everyone before using the admin report.')
        return
    message=await ctx.send('Loading Flux price drops...')
    try:
        report=await load(ctx.guild.id,draft_ref)
        await message.edit(content=None,embed=report_embed(report),view=ReportPages(report) if len(report['ranked'])>PAGE_SIZE else None,
                           allowed_mentions=discord.AllowedMentions.none())
    except ValueError as exc:
        await message.edit(content=str(exc),embed=None,view=None)
    except Exception:
        logging.getLogger(__name__).exception('Could not load private steal report')
        await message.edit(content='Could not read the Flux price sheet. Please try again.',embed=None,view=None)
