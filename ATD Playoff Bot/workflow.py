"""Bulk setup and fresh Google lineups, shared by buttons and legacy commands."""
import asyncio
import copy
import json
import os
from pathlib import Path
import re
from decimal import Decimal, InvalidOperation

import engine
import historical
import sheet_source

_setup_locks={}
_game_locks={}
_setup_epochs={}
_pending_setups={}
ROOT=Path(__file__).resolve().parent


def compact(value):
    return re.sub(r'[^a-z0-9]','',str(value).casefold())


def parse_request(text):
    parts=[p.strip() for p in text.split('|')]
    match=re.fullmatch(r'(?:ATD\s*)?(\d+)',parts[0],re.I)
    if not match or len(parts)>3:
        raise ValueError('Use !simsetup ATD 113. For a new voting sheet: !simsetup ATD 114 | <voting URL> [| roster tab].')
    return int(match[1]),parts[1] if len(parts)>1 else None,parts[2] if len(parts)>2 else None


def voting_source(text):
    match=re.search(r'/spreadsheets/d/([A-Za-z0-9_-]+)',text)
    key=match.group(1) if match else text
    if not re.fullmatch(r'[A-Za-z0-9_-]{20,150}',key):
        raise ValueError('Supply a valid voting spreadsheet URL or ID after the pipe.')
    gid=re.search(r'(?:[?&#])gid=(\d+)',text)
    return dict(sheet=key,gid=int(gid[1]) if gid else None)


def standings(rows):
    headers=[]
    for r,row in enumerate(rows):
        for c,value in enumerate(row):
            if compact(value)=='team' and [compact(x) for x in row[c+1:c+4]]==['wins','losses','gm']:
                headers.append((r,c))
    if not headers:
        return None
    if len(headers)!=1:
        raise ValueError('More than one standings table found. Use the voting tab URL with its gid.')
    r,c=headers[0];result=[];seen=set()
    for row in rows[r+1:]:
        cells=[str(row[i]).strip() if i<len(row) else '' for i in range(c,c+4)]
        if not any(cells):
            break
        name=cells[0].strip('[] ')
        try:
            wins,losses=Decimal(cells[1]),Decimal(cells[2])
        except InvalidOperation:
            raise ValueError('Final standings contain missing/invalid wins or losses. No setup was saved.') from None
        if not name or compact(name) in seen or not wins.is_finite() or not losses.is_finite() or min(wins,losses)<0:
            raise ValueError('Final standings contain duplicate or invalid teams. No setup was saved.')
        seen.add(compact(name));result.append(dict(name=name,wins=wins))
    if len(result)<16:
        raise ValueError('At least 16 completed seed records are required in the final standings table.')
    result.sort(key=lambda row:row['wins'],reverse=True)
    return [dict(seed=i+1,name=row['name']) for i,row in enumerate(result[:16])]


def team_logo(name,emojis):
    aliases=json.loads((ROOT/'team_aliases.json').read_text(encoding='utf-8'))
    aliases={compact(re.sub(r'~\d+$','',k)):compact(v) for k,v in aliases.items()}
    candidates=[e for e in emojis if aliases.get(compact(re.sub(r'~\d+$','',e['name'])),compact(e['name']))==compact(name)]
    if not candidates:
        raise ValueError(f'No server emoji matches {name}. Add its team emoji before setup; no generic logo was substituted.')
    candidates.sort(key=lambda e:(compact(e['name'])!=compact(name),len(e['name']),e['id']))
    e=candidates[0]
    return f"<{'a' if e.get('animated') else ''}:{e['name']}:{e['id']}>"


def read_setup(atd,source,emojis,credentials):
    client=sheet_source.client(credentials)
    voting=client.open_by_key(source['standings']['sheet'])
    gid=source['standings'].get('gid')
    tabs=[voting.get_worksheet_by_id(gid)] if gid is not None else voting.worksheets()
    found=[]
    for tab in tabs:
        seeds=standings(tab.get_all_values())
        if seeds:
            found.append((seeds,tab.id))
    if len(found)!=1:
        raise ValueError('Choose the voting tab containing one Team / Wins / Losses / GM standings table, including its gid in the URL.')
    seeds,source['standings']['gid']=found[0]
    book=client.open_by_key(source['sheet'])
    if not source.get('tab'):
        candidates=[w for w in book.worksheets() if re.search(r'\b'+str(atd)+r'\b',w.title)]
        real=[w for w in candidates if 'test' not in w.title.casefold()]
        candidates=real or candidates
        if len(candidates)!=1:
            raise ValueError('Specify the roster tab: !simsetup ATD '+str(atd)+' | <voting URL> | <roster tab>.')
        source['tab']=candidates[0].title
    rows=book.worksheet(source['tab']).get_all_values()
    teams=[dict(**row,logo=team_logo(row['name'],emojis),roster=sheet_source.parse_roster(rows,row['name'])) for row in seeds]
    names=[p['name'].strip().casefold() for t in teams for p in t['roster']]
    if len(set(names))!=160:
        raise ValueError('A player appears on multiple playoff teams. Correct the sheet before setup.')
    return teams,source


def source_for(db,atd,url,tab):
    defaults=json.loads((ROOT/'draft_sources.json').read_text(encoding='utf-8'))
    source=copy.deepcopy(db.draft_source(atd) or defaults.get(str(atd)))
    if source is None:
        source=dict(sheet=os.getenv('ROSTER_SPREADSHEET_ID','1WKGM-I0uvdKdZMKkF21GWxCmSdui76J-DUBVBNGco-U'),tab=None)
    if url:
        source['standings']=voting_source(url)
    if tab:
        source['tab']=tab
    if 'standings' not in source:
        raise ValueError(f'Use !simsetup ATD {atd} | <voting spreadsheet URL>. The voting source for this ATD is not registered yet.')
    return source


def cancel_pending(guild,atd):
    key=(guild,atd)
    if not _pending_setups.get(key):
        return False
    _setup_epochs[key]=_setup_epochs.get(key,0)+1
    return True


async def setup(db,guild,actor,text,emojis,credentials,cache,progress):
    atd,_,_=parse_request(text)
    key=(guild,atd);token=object();epoch=_setup_epochs.get(key,0)
    _pending_setups.setdefault(key,set()).add(token)
    def still_current():
        return _setup_epochs.get(key,0)==epoch
    async def checked_progress(message):
        if not still_current():
            raise ValueError('This setup was cancelled. No tournament was installed.')
        await progress(message)
    try:
        return await _setup(db,guild,actor,text,emojis,credentials,cache,checked_progress,still_current)
    finally:
        _pending_setups[key].discard(token)
        if not _pending_setups[key]:
            del _pending_setups[key]


async def _setup(db,guild,actor,text,emojis,credentials,cache,progress,still_current):
    atd,url,tab=parse_request(text)
    async with _setup_locks.setdefault(guild,asyncio.Lock()):
        try:
            active=db.season(guild)
        except ValueError:
            active=None
        if active:
            numbers=re.findall(r'\d+',active['name'])
            if active.get('atd',int(numbers[-1]) if numbers else None)!=atd:
                raise ValueError('Another ATD is already active. Its tournament was not changed.')
            if db.all_series(guild):
                return active,False,[]
        source=source_for(db,atd,url,tab)
        await progress('Reading the confirmed seeds, team logos and all 160 roster players...')
        teams,source=await asyncio.to_thread(read_setup,atd,source,emojis,credentials)
        old={p['name'].casefold()+'|'+p['year']:p['profile'] for t in (db.teams(active['id']) if active else []) for p in t['roster']}
        warnings=[]
        for i,team in enumerate(teams,1):
            for p in team['roster']:
                if p['name'].casefold()+'|'+p['year'] in old:
                    p['profile']=copy.deepcopy(old[p['name'].casefold()+'|'+p['year']])
            needs=[p for p in team['roster'] if p['profile']['source']=='estimated positional baseline']
            if needs:
                loaded,messages=await asyncio.to_thread(historical.sync,needs,cache)
                profiles={p['name']:p['profile'] for p in loaded}
                for p in team['roster']:
                    if p['name'] in profiles:
                        p['profile']=profiles[p['name']]
                warnings.extend(message for message in messages if ': loaded ' not in message)
            await progress(f'Imported team {i}/16: {team["name"]}. Preparing player baselines...')
        season,created=await asyncio.to_thread(db.install_setup,guild,atd,teams,source,actor,still_current)
        return season,created,warnings


def live_lineups(series,rows,cache):
    rosters=[]
    for team in (series['high'],series['low']):
        fresh=sheet_source.parse_roster(rows,team['name'])
        previous={(p['name'].strip().casefold(),p['year']):p for p in team['roster']}
        for p in fresh:
            old=previous.get((p['name'].strip().casefold(),p['year']))
            if old:
                p['profile']=copy.deepcopy(old['profile'])
        fresh=historical.cached(fresh,cache)
        rosters.append(fresh)
    return rosters


async def read_lineups(db,guild,sid,credentials,cache):
    s=db.series(guild,sid)
    season=db.season(guild)
    if season['id']!=s['season'] or not season.get('source'):
        raise ValueError('Configure the active roster sheet with !simsetup before playing. No game was simulated.')
    rows=await asyncio.to_thread(sheet_source.load_rows,season['source'],credentials)
    rosters=await asyncio.to_thread(live_lineups,s,rows,cache)
    return s,season['source'],rosters


async def play(db,guild,sid,number,actor,credentials,cache):
    async with _game_locks.setdefault((guild,sid),asyncio.Lock()):
        saved=next((g for g in db.games(guild,sid) if g['number']==number),None)
        if saved:
            return db.series(guild,sid),saved,False
        s=db.series(guild,sid)
        if s['complete'] or number!=sum(s['wins'])+1:
            raise ValueError('That game is not next, or the series has finished. Refresh the panel.')
        if s['base'] is None:
            raise ValueError('Set the voting odds using Series Settings first. No game was played.')
        s,source,rosters=await read_lineups(db,guild,sid,credentials,cache)
        await asyncio.to_thread(db.refresh_lineups,guild,sid,rosters,sum(s['wins']),source,actor)
        return await asyncio.to_thread(db.game,guild,sid,number,actor)
