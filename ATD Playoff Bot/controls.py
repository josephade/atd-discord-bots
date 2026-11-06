"""Commissioner controls: select series, settings, lineups and play buttons."""
import logging
from types import SimpleNamespace

import discord
import engine
import workflow
import memberships

log=logging.getLogger(__name__)


def authorized(api,interaction,private=False):
    if not interaction.guild or not api.admin(SimpleNamespace(author=interaction.user,guild=interaction.guild)):
        return False
    if api.GUILD_ID and interaction.guild.id!=api.GUILD_ID:
        return False
    if not api.channel_allowed(interaction.channel,interaction.channel_id):
        return False
    if private:
        return (interaction.channel_id in api.admin_channels() and
                not interaction.channel.permissions_for(interaction.guild.default_role).view_channel)
    return True


async def problem(interaction,error):
    text=str(error) if isinstance(error,ValueError) else 'The action could not finish. Use !sim to reopen the panel. Saved games remain saved.'
    if not isinstance(error,ValueError):
        log.error('Playoff panel failure',exc_info=(type(error),error,error.__traceback__))
    if interaction.response.is_done():
        await interaction.followup.send(text,ephemeral=True)
    else:
        await interaction.response.send_message(text,ephemeral=True)


async def play_game(api,interaction,sid,number,update_panel=False):
    gm_allowed=(interaction.guild is not None and api.channel_allowed(interaction.channel,interaction.channel_id)
                and (not api.GUILD_ID or interaction.guild.id==api.GUILD_ID)
                and api.store.can_play(interaction.guild.id,sid,interaction.user.id))
    if gm_allowed and getattr(interaction.channel,'parent_id',None) in api.PUBLIC_CHANNEL_IDS:
        try:
            gm_allowed=api.store.focus(interaction.guild.id,interaction.channel_id)==sid
        except ValueError:
            gm_allowed=False
    if not authorized(api,interaction) and not gm_allowed:
        await interaction.response.send_message('Only commissioners or registered GMs of this series can play its games.',ephemeral=True)
        return
    await interaction.response.defer()
    try:
        s,g,created=await workflow.play(api.store,interaction.guild.id,sid,number,interaction.user.id,
                                       api.credentials(),api.DATA_PATH.parent/'nba-baselines.sqlite3')
        await api.post_game(SimpleNamespace(send=interaction.followup.send),s,g,saved=not created)
        if update_panel:
            view=SeriesPanel(api,interaction.guild.id,sid)
            await interaction.message.edit(embed=view.embed(),view=view)
    except Exception as exc:
        await problem(interaction,exc)


class Settings(discord.ui.Modal):
    def __init__(self,panel):
        super().__init__(title=f'Series {panel.sid} settings')
        self.panel=panel;s=panel.series
        self.odds=discord.ui.TextInput(label=f"#{s['high']['seed']} base win chance (%)",default='' if s['base'] is None else str(s['base']/100),placeholder='47 means 47% before HCA',max_length=10)
        self.high=discord.ui.TextInput(label=f"#{s['high']['seed']} suspect deduction (0 or 10)",default=str(s['penalty_high']//100),max_length=2)
        self.low=discord.ui.TextInput(label=f"#{s['low']['seed']} suspect deduction (0 or 10)",default=str(s['penalty_low']//100),max_length=2)
        for field in (self.odds,self.high,self.low):
            self.add_item(field)

    async def on_submit(self,interaction):
        api=self.panel.api
        if not authorized(api,interaction,private=True):
            await interaction.response.send_message('Set odds and deductions in the private admin channel.',ephemeral=True)
            return
        try:
            values=[engine.percentage(str(field.value)) for field in (self.odds,self.high,self.low)]
            if any(p not in (0,1000) for p in values[1:]):
                raise ValueError('Suspect deductions must be 0 or 10 percentage points; they do not stack.')
            api.store.configure(interaction.guild.id,self.panel.sid,interaction.user.id,
                                base=values[0],penalty_high=values[1],penalty_low=values[2],votes_open=False,
                                odds_source='Commissioner-entered official voting odds')
            view=SeriesPanel(api,interaction.guild.id,self.panel.sid)
            await interaction.response.edit_message(embed=view.embed(),view=view)
        except Exception as exc:
            await problem(interaction,exc)


class TeamGMs(discord.ui.Modal):
    def __init__(self,panel):
        super().__init__(title=f'Series {panel.sid}: private GM access')
        self.panel=panel
        self.fields=[]
        for team in (panel.series['high'],panel.series['low']):
            field=discord.ui.TextInput(label=f"Seed {team['seed']}: GM IDs or mentions",required=False,
                default=', '.join(str(uid) for uid in panel.api.store.team_gms(panel.guild,team['seed'])),max_length=500)
            self.fields.append(field);self.add_item(field)

    async def on_submit(self,interaction):
        if not authorized(self.panel.api,interaction,private=True):
            await interaction.response.send_message('GM assignments are managed only in the private admin channel.',ephemeral=True)
            return
        try:
            values=[memberships.parse(str(field.value)) for field in self.fields]
            self.panel.api.store.set_series_gms(self.panel.guild,self.panel.sid,*values,interaction.user.id)
            await interaction.response.send_message('Saved private team GM access. Both partners can simulate their own series; odds/settings remain commissioner-only.',ephemeral=True)
        except Exception as exc:
            await problem(interaction,exc)


class SeriesPanel(discord.ui.View):
    def __init__(self,api,guild,sid=None):
        super().__init__(timeout=1800)
        self.api,self.guild=api,guild
        rows=api.store.all_series(guild)
        if not rows:
            raise ValueError('Use !simsetup ATD <number> to import the tournament first.')
        stage=max(s['stage'] for s in rows)
        self.current=[s for s in rows if s['stage']==stage]
        self.series=next((s for s in rows if s['id']==sid),self.current[0])
        self.sid=self.series['id']
        self.number=sum(self.series['wins'])+1
        self.selector.options=[discord.SelectOption(label=(f"#{s['high']['seed']} {s['high']['name']} vs #{s['low']['seed']} {s['low']['name']}")[:100],
            value=str(s['id']),description=f"Series {s['id']} | {s['wins'][0]}-{s['wins'][1]}"+(' FINAL' if s['complete'] else ''),default=s['id']==self.sid) for s in self.current]
        self.settings.disabled=sum(self.series['wins'])>0
        self.play.disabled=self.series['complete'] or self.series['base'] is None
        self.play.label=f'Play Game {self.number}' if not self.series['complete'] else 'Series finished'
        self.advance.disabled=stage==4 or not all(s['complete'] for s in self.current)
        self.stats.disabled=sum(self.series['wins'])==0

    def embed(self):
        e=self.api.series_embed(self.series,self.api.store.games(self.guild,self.sid))
        e.add_field(name='Controls',value='Choose a series above. Set odds/deductions, review lineups, then play. Google Sheets lineups refresh before every new game.',inline=False)
        return e

    async def interaction_check(self,interaction):
        if authorized(self.api,interaction):
            try:
                current=self.api.store.season(self.guild)
            except ValueError:
                current=None
            if current is None or current['id']!=self.series['season']:
                await interaction.response.send_message('This tournament is closed. Use !sim for the current tournament.',ephemeral=True)
                return False
            return True
        await interaction.response.send_message('These controls are for the owner and commissioners. Use !series or !playoffs to view results.',ephemeral=True)
        return False

    async def on_error(self,interaction,error,item):
        await problem(interaction,error)

    @discord.ui.select(placeholder='Choose a playoff series',row=0)
    async def selector(self,interaction,select):
        view=SeriesPanel(self.api,self.guild,int(select.values[0]))
        self.api.store.focus(self.guild,interaction.channel_id,view.sid)
        await interaction.response.edit_message(embed=view.embed(),view=view)

    @discord.ui.button(label='Series Settings',style=discord.ButtonStyle.primary,row=1)
    async def settings(self,interaction,button):
        if not authorized(self.api,interaction,private=True):
            await interaction.response.send_message('Open !sim in your private admin channel to set odds and deductions.',ephemeral=True)
            return
        # Re-read before opening: settings may have locked since this panel was sent.
        panel=SeriesPanel(self.api,self.guild,self.sid)
        if sum(panel.series['wins']):
            raise ValueError('This series has started. Voting odds and deductions are locked.')
        await interaction.response.send_modal(Settings(panel))

    @discord.ui.button(label='Review Live Lineups',style=discord.ButtonStyle.secondary,row=1)
    async def lineups(self,interaction,button):
        await interaction.response.defer(ephemeral=True)
        _,_,rosters=await workflow.read_lineups(self.api.store,self.guild,self.sid,self.api.credentials(),self.api.DATA_PATH.parent/'nba-baselines.sqlite3')
        e=discord.Embed(title='Current Google Sheets lineups',color=0x46DFB0)
        for team,roster in zip((self.series['high'],self.series['low']),rosters):
            lines=[]
            for i,p in enumerate(roster):
                label=engine.POSITIONS[i] if i<5 else f'Bench {i-4}'
                lines.append(f"**{label}** {p['name']} {p['year']}")
            e.add_field(name=team['name'],value='\n'.join(lines)[:1024],inline=False)
        e.set_footer(text='Read-only preview. The sheet is read again immediately before a new game.')
        await interaction.followup.send(embed=e,ephemeral=True)

    @discord.ui.button(label='Team GMs',style=discord.ButtonStyle.secondary,row=1)
    async def gms(self,interaction,button):
        if not authorized(self.api,interaction,private=True):
            await interaction.response.send_message('Open !sim in the private admin channel to manage GM assignments.',ephemeral=True)
            return
        await interaction.response.send_modal(TeamGMs(SeriesPanel(self.api,self.guild,self.sid)))

    @discord.ui.button(label='Play Next Game',style=discord.ButtonStyle.success,row=1)
    async def play(self,interaction,button):
        # The number is bound to this panel. Double-clicking cannot roll Game N+1.
        await play_game(self.api,interaction,self.sid,self.number,update_panel=True)

    @discord.ui.button(label='Series Averages',style=discord.ButtonStyle.secondary,row=2)
    async def stats(self,interaction,button):
        from io import BytesIO
        import asyncio
        import render
        await interaction.response.defer(ephemeral=True)
        s=self.api.store.series(self.guild,self.sid)
        raw=await asyncio.to_thread(render.stats_card,s,self.api.store.games(self.guild,self.sid))
        await interaction.followup.send(file=discord.File(BytesIO(raw),filename='series-averages.png'),ephemeral=True)

    @discord.ui.button(label='Bracket',style=discord.ButtonStyle.secondary,row=2)
    async def bracket(self,interaction,button):
        await interaction.response.send_message(embed=self.api.bracket_embed(self.guild),ephemeral=True)

    @discord.ui.button(label='Next Round',style=discord.ButtonStyle.primary,row=2)
    async def advance(self,interaction,button):
        if not authorized(self.api,interaction,private=True):
            raise ValueError('Advance rounds from the private admin channel.')
        latest=self.api.store.all_series(self.guild)
        if max(s['stage'] for s in latest)==max(s['stage'] for s in self.current):
            self.api.store.advance(self.guild,interaction.user.id)
        view=SeriesPanel(self.api,self.guild)
        await interaction.response.edit_message(embed=view.embed(),view=view)

    @discord.ui.button(label='Refresh',style=discord.ButtonStyle.secondary,row=2)
    async def refresh(self,interaction,button):
        view=SeriesPanel(self.api,self.guild,self.sid)
        await interaction.response.edit_message(embed=view.embed(),view=view)


async def send_panel(ctx,api,sid=None):
    view=SeriesPanel(api,ctx.guild.id,sid)
    api.store.focus(ctx.guild.id,ctx.channel.id,view.sid)
    await ctx.send(embed=view.embed(),view=view)
