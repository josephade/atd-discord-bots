"""Standalone ATD Playoff Bot. Importing this module never connects to Discord."""
import asyncio
import copy
from collections import OrderedDict
from io import BytesIO
import json
import logging
import os
from pathlib import Path
import re
import sys

import aiohttp
import discord
from discord.ext import commands
from dotenv import load_dotenv

import engine
import render
import sheet_source
import workflow
from store import Store

ROOT=Path(__file__).resolve().parent
load_dotenv(ROOT/'.env')
OWNER_ID=int(os.getenv('OWNER_ID') or '681833601830354944')
PRIVATE_CHANNEL_ID=int(os.getenv('PRIVATE_CHANNEL_ID') or '1533242104304631969')
BACKUP_ADMIN_CHANNEL_ID=int(os.getenv('BACKUP_ADMIN_CHANNEL_ID') or '939947875805302784')
GUILD_ID=int(os.getenv('DISCORD_GUILD_ID') or '0')
COMMISH_ROLE_ID=int(os.getenv('COMMISH_ROLE_ID') or '0')
VOTER_ROLE_ID=int(os.getenv('VOTER_ROLE_ID') or '0')
PUBLIC_CHANNEL_IDS={int(x.strip()) for x in (os.getenv('PUBLIC_CHANNEL_IDS') or '934054991965077564').split(',') if x.strip()}
DATA_PATH=Path(os.getenv('STATE_DIR',str(ROOT/'data')))/'playoffs.sqlite3'
store=Store(DATA_PATH)
intents=discord.Intents.default()
intents.message_content=True
bot=commands.Bot(command_prefix='!',intents=intents,help_command=None,allowed_mentions=discord.AllowedMentions.none())
log=logging.getLogger('atd.playoffs')
_logo_cache=OrderedDict()


def credentials():
    return os.getenv('SERVICE_ACCOUNT_FILE','service_account.json')


def admin_channels():
    return {PRIVATE_CHANNEL_ID,BACKUP_ADMIN_CHANNEL_ID}


def channel_allowed(channel,channel_id=None):
    """Allow the sim channel, its threads, and the private admin channel."""
    cid=channel_id if channel_id is not None else channel.id
    return cid in PUBLIC_CHANNEL_IDS|admin_channels() or getattr(channel,'parent_id',None) in PUBLIC_CHANNEL_IDS


def admin(ctx):
    return ctx.author.id==OWNER_ID or (ctx.guild and (ctx.author.guild_permissions.manage_guild or
           ctx.author.guild_permissions.administrator or any(r.id==COMMISH_ROLE_ID for r in ctx.author.roles)))


def commissioner(private=True):
    async def predicate(ctx):
        if not ctx.guild or not admin(ctx):
            raise commands.CheckFailure('Only the owner or commissioners can run this command in the server.')
        if private and ctx.channel.id not in admin_channels():
            raise commands.CheckFailure(f'Use <#{PRIVATE_CHANNEL_ID}> or <#{BACKUP_ADMIN_CHANNEL_ID}> for admin setup. Nothing was changed.')
        if private and ctx.channel.permissions_for(ctx.guild.default_role).view_channel:
            raise commands.CheckFailure('Hide the admin channel from @everyone before running setup commands.')
        return True
    return commands.check(predicate)


@bot.check
async def channel_check(ctx):
    if ctx.guild and GUILD_ID and ctx.guild.id!=GUILD_ID:
        raise commands.CheckFailure('This bot is configured for another server.')
    if ctx.guild and not channel_allowed(ctx.channel):
        raise commands.CheckFailure('Use <#934054991965077564>, a thread inside it, or the private admin channel.')
    return True


def guild_id(ctx):
    if ctx.guild:
        return ctx.guild.id
    if GUILD_ID:
        return GUILD_ID
    if len(bot.guilds)==1:
        return bot.guilds[0].id
    raise ValueError('Use this command in the server; DM commands need DISCORD_GUILD_ID configured.')


async def voter(ctx):
    guild=ctx.guild or bot.get_guild(guild_id(ctx))
    if guild is None:
        raise ValueError('The configured server is unavailable.')
    member=guild.get_member(ctx.author.id)
    if member is None:
        try:
            member=await guild.fetch_member(ctx.author.id)
        except discord.NotFound:
            raise ValueError('You must belong to the draft’s server to vote.') from None
    if VOTER_ROLE_ID and not any(role.id==VOTER_ROLE_ID for role in member.roles):
        raise ValueError('You need the configured voter role to submit a ballot.')
    return guild.id


def choose(ctx,sid=None):
    return sid or store.focus(guild_id(ctx),ctx.channel.id)


def choose_play(ctx,sid=None):
    gid=guild_id(ctx)
    threaded=getattr(ctx.channel,'parent_id',None) in PUBLIC_CHANNEL_IDS
    if threaded:
        try:
            bound=store.focus(gid,ctx.channel.id)
        except ValueError:
            if not admin(ctx) or sid is None:
                raise commands.CheckFailure('A commissioner needs to open !sim in this thread and select the matchup first.') from None
            bound=store.focus(gid,ctx.channel.id,sid)
        if sid is not None and sid!=bound:
            raise commands.CheckFailure('Use the thread linked to that series.')
        sid=bound
    elif sid is None:
        if admin(ctx):
            sid=choose(ctx)
        else:
            matches=store.gm_series(gid,ctx.author.id)
            if len(matches)!=1:
                raise commands.CheckFailure('Your series is not linked yet. Ask a commissioner to register your team in Team GMs and select its series in the match thread.')
            sid=matches[0]['id']
    if not admin(ctx) and not store.can_play(gid,sid,ctx.author.id):
        raise commands.CheckFailure('Only a commissioner or a registered GM in this series can simulate it.')
    return sid


def pct(value):
    return f'{value/100:g}%'


def team_text(team):
    return f"{team['logo']} **{discord.utils.escape_markdown(team['name'])}** (#{team['seed']})"


def series_embed(s,games=None):
    embed=discord.Embed(title=('TEST · ' if s.get('is_test') else '')+f"Series {s['id']} · {'Finals' if s['stage']==4 else 'Round '+str(s['stage'])}",color=0x46DFB0)
    embed.description=team_text(s['high'])+'\nvs\n'+team_text(s['low'])
    embed.add_field(name='Series',value=f"{s['wins'][0]}–{s['wins'][1]}"+(' · FINAL' if s['complete'] else ' · Best of seven'))
    embed.add_field(name='Voting odds (before HCA)',value=f"{pct(s['base'])} / {pct(10000-s['base'])}" if s['base'] is not None else 'Awaiting official odds / ballots')
    embed.add_field(name='HCA',value='Higher seed: Games 1, 2, 5, 7\nHome team: +10 percentage points',inline=False)
    embed.add_field(name='Suspect deductions',value=f"Higher seed −{s['penalty_high']/100:g} percentage points\nLower seed −{s['penalty_low']/100:g} percentage points")
    if not s['complete'] and s['base'] is not None:
        number=sum(s['wins'])+1
        high=engine.probability(s['base'],number,s['penalty_high'],s['penalty_low'],s['hca'])
        embed.add_field(name=f'Game {number} final chances',value=f'{pct(high)} / {pct(10000-high)}')
    if games:
        embed.add_field(name='Results',value='\n'.join(f"G{g['number']}: {g['scores'][0]}–{g['scores'][1]}"+(' OT' if g['ot'] else '') for g in games),inline=False)
    embed.set_footer(text='Odds listed higher seed / lower seed. Player stats are simulated. '+str(s['odds_source'] or ''))
    return embed


async def emoji_image(logo):
    match=re.fullmatch(r'<a?:[^:]+:(\d+)>',logo)
    if not match:
        return None
    key=match.group(1)
    if key in _logo_cache:
        _logo_cache.move_to_end(key)
        return _logo_cache[key]
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
            async with session.get(f'https://cdn.discordapp.com/emojis/{key}.png?size=128') as response:
                if response.status!=200:
                    return None
                data=await response.read()
                if len(data)>1024*1024:
                    return None
                _logo_cache[key]=data
                while len(_logo_cache)>64:
                    _logo_cache.popitem(last=False)
                return data
    except (aiohttp.ClientError,asyncio.TimeoutError):
        return None


class GamePages(discord.ui.View):
    def __init__(self,s,g):
        super().__init__(timeout=900)
        self.s,self.g=s,g
        self.next_game.disabled=g.get('series_complete',False)

    async def page(self,interaction,side=None,stats=False):
        await interaction.response.defer()
        if stats:
            games=await asyncio.to_thread(store.games,self.s['guild'],self.s['id'])
            data=await asyncio.to_thread(render.stats_card,self.s,games)
        elif side is not None:
            data=await asyncio.to_thread(render.box_card,self.s,self.g,side)
        else:
            logos=await asyncio.gather(emoji_image(self.s['high']['logo']),emoji_image(self.s['low']['logo']))
            data=await asyncio.to_thread(render.game_card,self.s,self.g,logos)
        # Each click opens a private page; another GM cannot change your view.
        await interaction.followup.send(file=discord.File(BytesIO(data),filename='playoffs.png'),ephemeral=True)

    @discord.ui.button(label='Higher Seed Box',style=discord.ButtonStyle.primary)
    async def high(self,interaction,button):
        await self.page(interaction,0)

    @discord.ui.button(label='Lower Seed Box',style=discord.ButtonStyle.primary)
    async def low(self,interaction,button):
        await self.page(interaction,1)

    @discord.ui.button(label='Series Averages',style=discord.ButtonStyle.secondary)
    async def stats(self,interaction,button):
        await self.page(interaction,stats=True)

    @discord.ui.button(label='Play Next Game',style=discord.ButtonStyle.success)
    async def next_game(self,interaction,button):
        import controls
        await controls.play_game(sys.modules[__name__],interaction,self.s['id'],self.g['number']+1)

    @discord.ui.button(label='Series Controls',style=discord.ButtonStyle.secondary)
    async def controls(self,interaction,button):
        import controls
        if not controls.authorized(sys.modules[__name__],interaction):
            await interaction.response.send_message('Series controls are for commissioners.',ephemeral=True)
            return
        view=controls.SeriesPanel(sys.modules[__name__],interaction.guild.id,self.s['id'])
        await interaction.response.send_message(embed=view.embed(),view=view,ephemeral=True)


async def post_game(ctx,s,g,saved=False):
    if g.get('rosters'):
        s=copy.deepcopy(s)
        s['high']['roster'],s['low']['roster']=g['rosters']
    logos=await asyncio.gather(emoji_image(s['high']['logo']),emoji_image(s['low']['logo']))
    data=await asyncio.to_thread(render.game_card,s,g,logos)
    winner=s['high'] if g['winner']==0 else s['low']
    star=g['star']
    description=(f"{team_text(winner)} wins **{max(g['scores'])}–{min(g['scores'])}**"+(' in OT.' if g['ot'] else '.')+
        f"\n**{star['name']}**: {star['pts']} PTS · {star['reb']} REB · {star['ast']} AST.\n"+
        f"Series: **{g['series_wins'][0]}–{g['series_wins'][1]}**"+(' · Series finished.' if g['series_complete'] else '')+
        f"\nFinal game odds: **{pct(g['odds_high'])} / {pct(10000-g['odds_high'])}** (higher/lower seed).")
    embed=discord.Embed(title=('TEST · ' if s.get('is_test') else '')+f"Game {g['number']} · Series {s['id']}"+(' · Saved result' if saved else ''),description=description,color=0x46DFB0)
    estimated=sum(p['profile']['source']=='estimated positional baseline' for t in (s['high'],s['low']) for p in t['roster'])
    embed.set_footer(text=f'Simulated stats · {estimated}/20 estimated player baselines · Voting odds determine the winner')
    embed.set_image(url='attachment://game.png')
    await ctx.send(embed=embed,file=discord.File(BytesIO(data),filename='game.png'),view=GamePages(s,g))


@bot.command()
@commissioner()
async def simsetup(ctx,*,text: str):
    import controls
    message=await ctx.send('Preparing the playoff tournament...')
    async def progress(text):
        await message.edit(content=text)
    try:
        emojis=[dict(name=e.name,id=str(e.id),animated=e.animated) for e in await ctx.guild.fetch_emojis()]
        season,created,warnings=await workflow.setup(store,ctx.guild.id,ctx.author.id,text,emojis,credentials(),
                                                    DATA_PATH.parent/'nba-baselines.sqlite3',progress)
        view=controls.SeriesPanel(sys.modules[__name__],ctx.guild.id)
        store.focus(ctx.guild.id,ctx.channel.id,view.sid)
        estimated=sum(p['profile']['source']=='estimated positional baseline' for t in store.teams(season['id']) for p in t['roster'])
        note=(f"**{season['name']} is ready.** Imported 16 teams, 160 players and the bracket." if created else
              f"**{season['name']} controls.** Existing seeds, odds and games were preserved.")
        note+=f'\n{estimated}/160 estimated player baselines. Choose a series, open **Series Settings**, then **Play Game**. Live lineups refresh before each new game.'
        await message.edit(content=note,embed=view.embed(),view=view)
        if warnings:
            await ctx.send('Some player-season baselines were unavailable; estimates are labelled. See the attached setup report.',
                           file=discord.File(BytesIO('\n'.join(warnings).encode()),filename='baseline-import-report.txt'))
    except Exception as exc:
        if not isinstance(exc,ValueError):
            log.exception('Automatic playoff setup failed')
        await message.edit(content=str(exc) if isinstance(exc,ValueError) else 'Could not finish loading the sheets. No partial tournament was installed; try !simsetup again.',embed=None,view=None)


@bot.command()
@commissioner()
async def simcreate(ctx,*,name: str):
    if re.fullmatch(r'(?:ATD\s*)?\d+(?:\s*\|.*)?',name.strip(),re.I):
        await simsetup.callback(ctx,text=name)
        return
    s=await asyncio.to_thread(store.create,ctx.guild.id,name,ctx.author.id)
    await ctx.send(f"Created **{s['name']}**. Register seeds 1–16 using `!simteam <seed> <emoji> <team name>`.")


@bot.command()
@commissioner()
async def simtest(ctx,*,name: str):
    s=await asyncio.to_thread(store.create,ctx.guild.id,'TEST - '+name,ctx.author.id,True)
    await ctx.send(f"Created **{s['name']}**. Register seeds and rosters as usual. Test results are labelled, and `!simarchive` can end this test at any time while preserving its saved games.")


@bot.command()
@commissioner()
async def simteam(ctx,seed: int,logo: str,*,name: str):
    t=await asyncio.to_thread(store.save_team,ctx.guild.id,seed,logo,name,ctx.author.id)
    await ctx.send(f"Saved {team_text(t)}. Next: `!simsetroster {seed}` or `!simimport {seed}`.")


@bot.command()
@commissioner()
async def simsetroster(ctx,seed: int,*,text: str):
    rows=[]
    for i,line in enumerate(text.strip().splitlines()):
        parts=[p.strip() for p in line.split('|')]
        if len(parts)!=2:
            raise ValueError('Use one player per line: Player Name | 1995-96. Supply exactly 10 lines in roster-slot order.')
        rows.append(dict(name=parts[0],year=parts[1],position=engine.POSITIONS[min(i,9)]))
    engine.validate_roster(rows)
    t=store.team(ctx.guild.id,seed)
    await asyncio.to_thread(store.save_team,ctx.guild.id,seed,t['logo'],t['name'],ctx.author.id,rows)
    await ctx.send(f"Saved 10 players for {team_text(t)}. Stats currently use estimated positional baselines; `!simprofile` can replace them.")


@bot.command()
@commissioner()
async def simsheet(ctx,*,text: str):
    await asyncio.to_thread(store.save_source,ctx.guild.id,sheet_source.source(text),ctx.author.id)
    await ctx.send('Saved the read-only roster source. Use `!simimport <seed>` for each team before creating the bracket.')


@bot.command()
@commissioner()
async def simimport(ctx,seed: int):
    season=store.season(ctx.guild.id)
    if not season['source']:
        raise ValueError('Set !simsheet <URL> | <worksheet name> first.')
    t=store.team(ctx.guild.id,seed)
    async with ctx.typing():
        rows=await asyncio.to_thread(sheet_source.load,season['source'],t['name'],os.getenv('SERVICE_ACCOUNT_FILE','service_account.json'))
        await asyncio.to_thread(store.save_team,ctx.guild.id,seed,t['logo'],t['name'],ctx.author.id,rows)
    await ctx.send(f"Imported 10 player names/seasons for {team_text(t)}. Season statistics are not in the roster sheet; baselines are estimated until configured with `!simprofile`.")


@bot.command()
@commissioner()
async def simprofile(ctx,seed: int,slot: int,ppg: float,rpg: float,apg: float):
    if slot not in range(1,11):
        raise ValueError('Slot is 1–10: starters PG through C, then Bench 1–5.')
    t=store.team(ctx.guild.id,seed)
    original=copy.deepcopy(t['roster'])
    if len(t['roster'])!=10:
        raise ValueError('Set the ten-player roster first.')
    t['roster'][slot-1]['profile']=dict(pts=ppg,reb=rpg,ast=apg,source='commissioner-entered baseline')
    engine.validate_roster(t['roster'])
    await asyncio.to_thread(store.save_team,ctx.guild.id,seed,t['logo'],t['name'],ctx.author.id,t['roster'],original)
    await ctx.send('Saved the player’s scoring/rebounding/assist baseline. It affects the generated stat distribution, never the winning odds.')


@bot.command()
@commissioner()
async def simloadstats(ctx,seed: int):
    import historical
    t=store.team(ctx.guild.id,seed)
    if len(t['roster'])!=10:
        raise ValueError('Set all ten roster players before loading season baselines.')
    if store.all_series(ctx.guild.id):
        raise ValueError('Load player stats before creating the bracket.')
    message=await ctx.send('Loading exact player-season baselines. Unavailable seasons keep their existing baseline.')
    async with ctx.typing():
        rows,report=await asyncio.to_thread(historical.sync,t['roster'],DATA_PATH.parent/'nba-baselines.sqlite3')
        await asyncio.to_thread(store.save_team,ctx.guild.id,seed,t['logo'],t['name'],ctx.author.id,rows,t['roster'])
    await message.edit(content='**Player baseline report**\n'+'\n'.join(report),allowed_mentions=discord.AllowedMentions.none())


@bot.command()
@commissioner()
async def simbracket(ctx):
    import controls
    if store.all_series(ctx.guild.id):
        await controls.send_panel(ctx,sys.modules[__name__])
        return
    rows=await asyncio.to_thread(store.bracket,ctx.guild.id,ctx.author.id)
    await ctx.send('Bracket ready. Starting lineups refresh from Google Sheets before every new game.')
    await controls.send_panel(ctx,sys.modules[__name__],rows[0]['id'])


@bot.command()
@commissioner()
async def simresetbracket(ctx,confirmation: str=''):
    if confirmation!='CONFIRM':
        raise ValueError('Use !simresetbracket CONFIRM to remove the unplayed bracket and its ballots, allowing roster corrections. Any saved game blocks this command.')
    await asyncio.to_thread(store.reset_bracket,ctx.guild.id,ctx.author.id)
    await ctx.send('Unplayed bracket and its ballots cleared. Rosters can be corrected. Rebuild with !simbracket.')


@bot.command()
@commissioner()
async def simodds(ctx,sid: int,higher_seed_percent: str):
    base=engine.percentage(higher_seed_percent)
    s=await asyncio.to_thread(store.configure,ctx.guild.id,sid,ctx.author.id,base=base,votes_open=False,odds_source='Commissioner-entered official voting odds')
    await ctx.send(embed=series_embed(s))


@bot.command()
@commissioner()
async def simpenalty(ctx,sid: int,seed: int,points: str='10'):
    s=store.series(ctx.guild.id,sid)
    if seed not in (s['high']['seed'],s['low']['seed']):
        raise ValueError('Choose one of the two seeds playing in this series.')
    amount=engine.percentage(points)
    if amount not in (0,1000):
        raise ValueError('Use 10 for a suspect deduction, or 0 to correct it before Game 1. Deductions cannot stack.')
    key='penalty_high' if seed==s['high']['seed'] else 'penalty_low'
    s=await asyncio.to_thread(store.configure,ctx.guild.id,sid,ctx.author.id,**{key:amount})
    await ctx.send(embed=series_embed(s))


@bot.command()
@commissioner(False)
async def simfocus(ctx,sid: int):
    await asyncio.to_thread(store.focus,ctx.guild.id,ctx.channel.id,sid)
    await ctx.send(f'This channel now uses **Series {sid}**. Run `!series` to view it.')


@bot.command()
@commissioner()
async def simclosevotes(ctx,sid: int):
    s=await asyncio.to_thread(store.close_votes,ctx.guild.id,sid,ctx.author.id)
    await ctx.send(embed=series_embed(s))


@bot.command()
@commissioner()
async def simadvance(ctx):
    rows=await asyncio.to_thread(store.advance,ctx.guild.id,ctx.author.id)
    await ctx.send('Next round created. Enter voting odds for each new series.\n'+'\n'.join(f"**Series {s['id']}**: #{s['high']['seed']} vs #{s['low']['seed']}" for s in rows))


@bot.command()
async def sim(ctx,*,text: str=''):
    if not ctx.guild:
        raise commands.CheckFailure('Use !sim in #atd-sims or its match thread.')
    if not text.strip():
        if admin(ctx):
            import controls
            await controls.send_panel(ctx,sys.modules[__name__])
        else:
            sid=choose_play(ctx)
            await ctx.send(embed=series_embed(store.series(ctx.guild.id,sid),store.games(ctx.guild.id,sid)))
        return
    match=re.fullmatch(r'(?:game\s*)?([1-7])(?:\s+(\d+))?',text.strip(),re.I)
    if not match:
        raise ValueError('Use !sim Game 1 [series ID]. The series ID is optional after !simfocus.')
    number=int(match[1]);sid=choose_play(ctx,int(match[2]) if match[2] else None)
    async with ctx.typing():
        s,g,created=await workflow.play(store,ctx.guild.id,sid,number,ctx.author.id,credentials(),DATA_PATH.parent/'nba-baselines.sqlite3')
        await post_game(ctx,s,g,saved=not created)


@bot.command()
@commissioner()
async def simaudit(ctx,sid: int,number: int):
    s,g,_=store.game(ctx.guild.id,sid,number)
    if g.get('rosters'):
        s=copy.deepcopy(s)
        s['high']['roster'],s['low']['roster']=g['rosters']
    regenerated=engine.simulate(s,number,g['seed'])
    consistent=all(g[k]==regenerated[k] for k in ('scores','quarters','boxes','roll','odds_high'))
    embed=discord.Embed(title=f'Series {sid} · Game {number} audit',color=0x46DFB0)
    embed.description=(f"Engine **{g['version']}** · Reproduction: **{'PASS' if consistent else 'MISMATCH'}**\n"
                       f"Roll: **{g['roll']}/10000**. Higher seed wins when roll ≤ **{g['odds_high']}**.\n"
                       f"Saved UTC: {g['created']}\nRandom seed: `{g['seed']}`")
    await ctx.send(embed=embed)


@bot.command()
@commissioner()
async def simexport(ctx):
    data=await asyncio.to_thread(store.export,ctx.guild.id)
    raw=json.dumps(data,indent=2,ensure_ascii=False).encode()
    await ctx.send('Private export includes ballots, saved random seeds and commissioner audit history.',file=discord.File(BytesIO(raw),filename='playoff-export.json'))


@bot.command()
@commissioner()
async def simarchive(ctx):
    await asyncio.to_thread(store.archive,ctx.guild.id,ctx.author.id)
    await ctx.send('Archived the season. Results remain saved. Use !simcreate for a real tournament or !simtest for another test; archived games remain accessible by series ID.')


@bot.command()
@commissioner()
async def simcancel(ctx,*,text: str=''):
    match=re.fullmatch(r'ATD\s+(\d+)',text.strip(),re.I)
    if not match:
        raise ValueError('Use !simcancel ATD <number>, for example !simcancel ATD 113. Saved games will be kept.')
    atd=int(match[1])
    pending=workflow.cancel_pending(ctx.guild.id,atd)
    try:
        season=await asyncio.to_thread(store.cancel,ctx.guild.id,atd,ctx.author.id)
    except ValueError:
        if not pending:
            raise
        await ctx.send(f'Cancelled the pending ATD {atd} setup. Other tournaments and saved games are unchanged. You can start a new ATD with !simcreate ATD <number>.')
        return
    await ctx.send(f"**Cancelled {season['name']}.** No further games can be played in that tournament. Saved games, box scores and audit history are kept.\nStart the next tournament with `!simcreate ATD <number>`.")


@bot.command()
async def simvote(ctx,sid: int,choice: int):
    gid=await voter(ctx)
    await asyncio.to_thread(store.vote,gid,sid,ctx.author.id,choice)
    await ctx.send('Your latest ballot is saved. You can replace it until voting closes.\n0/1/2/3: higher seed in 4/5/6/7. 4/5/6/7: lower seed in 7/6/5/4.')


@bot.command(name='series')
async def show_series(ctx,sid: int=None):
    sid=choose(ctx,sid)
    await ctx.send(embed=series_embed(store.series(guild_id(ctx),sid),store.games(guild_id(ctx),sid)))


def bracket_embed(gid):
    rows=store.all_series(gid)
    embed=discord.Embed(title=store.season(gid)['name']+' · Playoffs',color=0x46DFB0)
    for stage,label in enumerate(('First Round','Quarterfinals','Semifinals','Finals'),1):
        current=[s for s in rows if s['stage']==stage]
        if current:
            embed.add_field(name=label,value='\n'.join(f"**S{s['id']}** · {s['high']['logo']} #{s['high']['seed']} **{s['wins'][0]}–{s['wins'][1]}** #{s['low']['seed']} {s['low']['logo']}"+(' · FINAL' if s['complete'] else '') for s in current),inline=False)
    if not rows:
        embed.description='The bracket is not ready yet.'
    final=next((s for s in rows if s['stage']==4 and s['complete']),None)
    if final:
        embed.add_field(name='CHAMPION',value=team_text(final['high'] if final['wins'][0]==4 else final['low']),inline=False)
    return embed


@bot.command(aliases=['playoff','playofffs'])
async def playoffs(ctx):
    await ctx.send(embed=bracket_embed(guild_id(ctx)))


@bot.command(aliases=['boxscores'])
async def boxscore(ctx,number: int,sid: int=None):
    s,g,_=store.game(guild_id(ctx),choose(ctx,sid),number)
    await post_game(ctx,s,g,saved=True)


@bot.command()
async def seriesstats(ctx,sid: int=None):
    sid=choose(ctx,sid)
    s=store.series(guild_id(ctx),sid)
    data=await asyncio.to_thread(render.stats_card,s,store.games(guild_id(ctx),sid))
    await ctx.send(file=discord.File(BytesIO(data),filename='series-averages.png'))


@bot.command()
async def simroster(ctx,seed: int):
    t=store.team(guild_id(ctx),seed)
    source=store.season(guild_id(ctx)).get('source')
    if source:
        t['roster']=await asyncio.to_thread(sheet_source.load,source,t['name'],credentials())
    embed=discord.Embed(title=t['name']+f' · #{seed} seed',color=0x46DFB0)
    lines=[]
    for i,p in enumerate(t['roster']):
        slot=engine.POSITIONS[i] if i<5 else f'Bench {i-4}'
        lines.append(f"**{slot}** · {p['name']} {p['year']}")
    embed.description='\n'.join(lines) or 'Roster not entered yet.'
    embed.set_footer(text='Live Google Sheets lineup. No GM identities shown.' if source else 'Saved roster. No Google roster sheet is configured.')
    await ctx.send(embed=embed)


@bot.command()
async def simhelp(ctx,mode: str=''):
    if mode.casefold()!='advanced':
        embed=discord.Embed(title='ATD Sim Bot',color=0x46DFB0,
            description='**Setup once:** `!simcreate ATD 113`\n**Open the controls:** `!sim`\nChoose a series, enter voting odds and deductions in **Series Settings**, review live lineups, then use **Play Game** and **Next Round**.\n\nFor a new voting sheet: `!simcreate ATD 114 | <voting URL>`\nGoogle lineups refresh before each new game. Saved games cannot be rerolled.')
        if not admin(ctx):
            embed.description='In your linked match thread, use `!sim Game 1`, then the next game number. You must be a registered GM of one of the teams. A commissioner sets the odds and deductions first. `!sim` shows your series. Saved games cannot be rerolled.'
        else:
            embed.description+='\nUse **Team GMs** privately to register each team’s GMs. In the match thread open !sim and select the matchup once; its GMs can then use !sim Game 1.'
        embed.add_field(name='View results',value='`!playoffs` · bracket\n`!series [ID]` · matchup\n`!boxscore <game> [ID]` · saved game\n`!seriesstats [ID]` · averages\n`!simroster <seed>` · players\n`!simvote <ID> <0-7>` · optional ballots',inline=False)
        if admin(ctx):
            embed.add_field(name='End a tournament',value='`!simcancel ATD <number>` · stop early and allow a new ATD; saved games are kept.\n`!simarchive` · archive completed playoffs.',inline=False)
        embed.set_footer(text='Use #atd-sims or its threads. !simhelp advanced shows the optional manual controls.')
        await ctx.send(embed=embed)
        return
    embed=discord.Embed(title='ATD Playoff Bot',description='Voting odds decide the winner. Box scores are simulated. Saved games cannot be rerolled.',color=0x46DFB0)
    embed.add_field(name='GM commands',value='`!sim Game <1-7> [ID]` · registered GMs: own series only\n`!sim` · your series\n`!playoffs` · bracket\n`!series [ID]` · matchup, odds and results\n`!simvote <ID> <0-7>` · latest ballot (DM supported)\n`!boxscore <game> [ID]` / `!boxscores` · saved game\n`!seriesstats [ID]` · simulated averages\n`!simroster <seed>` · roster\n`!simhelp` · this help',inline=False)
    if admin(ctx):
        embed.add_field(name='Admin setup (private channel)',value='`!simsetup ATD <number>` · automatic setup\n`!simcreate <ATD name>`\n`!simtest <ATD name>` · labelled test; archive at any time\n`!simteam <seed> <emoji> <name>`\n`!simsetroster <seed>` + 10 `Name | Season` lines\n`!simsheet <URL> | <tab>` · `!simimport <seed>`\n`!simloadstats <seed>` · exact-season NBA baselines\n`!simprofile <seed> <slot> <PPG> <RPG> <APG>`\n`!simbracket`\n`!simresetbracket CONFIRM` · only before any game',inline=False)
        embed.add_field(name='Admin series controls',value='`!simodds <ID> <higher-seed %>`\n`!simclosevotes <ID>`\n`!simpenalty <ID> <seed> [10 or 0]`\n`!simfocus <ID>` · choose this channel/thread’s series\n`!sim Game <1-7> [ID]` · play next game\n`!simadvance` · create next round\n`!simaudit <ID> <game>` · `!simexport`\n`!simarchive` · archive completed season\n`!simcancel ATD <number>` · stop early, keep saved games\nOnly `!sim` and `!simfocus` can run outside the private setup channel.',inline=False)
    await ctx.send(embed=embed)


@bot.event
async def on_ready():
    log.info('Connected as %s; database %s',bot.user,DATA_PATH)
    await bot.change_presence(activity=discord.Game('ATD Playoffs | !simhelp'))
    for guild in bot.guilds:
        if GUILD_ID and guild.id!=GUILD_ID:
            continue
        try:
            for thread in await guild.active_threads():
                await join_sim_thread(thread)
        except discord.HTTPException:
            log.warning('Could not join existing sim threads in guild %s',guild.id)


async def join_sim_thread(thread):
    if thread.parent_id not in PUBLIC_CHANNEL_IDS or (GUILD_ID and thread.guild.id!=GUILD_ID):
        return
    try:
        await thread.join()
    except discord.HTTPException:
        log.warning('Could not join sim thread %s. Private threads require a bot invitation.',thread.id)


@bot.event
async def on_thread_create(thread):
    await join_sim_thread(thread)


@bot.event
async def on_command_error(ctx,error):
    if isinstance(error,commands.CommandNotFound):
        return
    if isinstance(error,commands.MissingRequiredArgument):
        usage=str(ctx.command.signature)
        message=f'Usage: `!{ctx.command.qualified_name} {usage}`. Use `!simhelp` for examples.'
    elif isinstance(error,commands.BadArgument):
        message='Check the seed, series ID, game number or numeric value. Use `!simhelp` for command formats.'
    elif isinstance(error,commands.CheckFailure):
        message=str(error)
    else:
        original=getattr(error,'original',error)
        if isinstance(original,ValueError):
            message=str(original)
        else:
            log.error('Command failed: %s',ctx.command,exc_info=(type(original),original,original.__traceback__))
            message='The command could not finish. A simulated game may already be saved; use !boxscore to check before retrying.'
    await ctx.send(message,allowed_mentions=discord.AllowedMentions.none())


if __name__=='__main__':
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    token=os.getenv('DISCORD_TOKEN','').strip()
    if not token:
        raise SystemExit('Set DISCORD_TOKEN in ATD Playoff Bot/.env. See README.md for setup. Never reuse another bot’s token.')
    bot.run(token)
