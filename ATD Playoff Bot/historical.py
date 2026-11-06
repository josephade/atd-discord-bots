"""Optional exact-name, exact-season NBA baselines. Failure is explicit.

Fetched during setup, never while simulating games. Cached career rows live
on the persistent volume. No fuzzy matches or silent substitution of seasons.
"""
import json
from pathlib import Path
import re
import sqlite3
import threading
import time
import unicodedata
from contextlib import closing

_lock=threading.Lock()


def normalized(value):
    return re.sub(r'[^a-z0-9]','',unicodedata.normalize('NFKD',value).encode('ascii','ignore').decode().lower())


def season(value):
    value=value.strip().replace('’',"'").replace('‘',"'")
    if re.fullmatch(r'\d{4}-\d{2}',value):
        start=int(value[:4]);end=int(value[-2:])
        if (start+1)%100==end:
            return value
    if re.fullmatch(r"'?\d{2}|\d{4}",value):
        end=int(value.lstrip("'"))
        if end<100:
            end+=1900 if end>=47 else 2000
        return f'{end-1}-{end%100:02d}'
    raise ValueError('Use an exact season such as 1995-96 or an ending year such as 1996.')


def profile(rows,year):
    chosen=[r for r in rows if r.get('SEASON_ID')==season(year) and str(r.get('LEAGUE_ID','00')).zfill(2)=='00']
    if len(chosen)>1:
        chosen=[r for r in chosen if r.get('TEAM_ID')==0 or r.get('TEAM_ABBREVIATION')=='TOT']
    if len(chosen)!=1:
        raise ValueError('No unique NBA season total found. ABA-only seasons need a manual !simprofile baseline.')
    row=chosen[0]
    return dict(pts=float(row['PTS']),reb=float(row['REB']),ast=float(row['AST']),
                three_share=(float(row.get('FG3M') or 0)/float(row['FGM']) if row.get('FGM') else 0),
                source='NBA PlayerCareerStats PerGame '+season(year))


def cached(players,path):
    """Use stored career data for changed years without network calls on game day."""
    if not Path(path).is_file():
        return players
    from nba_api.stats.static import players as static
    index={}
    for p in static.get_players():
        index.setdefault(normalized(p['full_name']),[]).append(p)
    with closing(sqlite3.connect(f'file:{Path(path).as_posix()}?mode=ro',uri=True)) as con:
        if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='careers'").fetchone():
            return players
        for p in players:
            if p['profile']['source']!='estimated positional baseline':
                continue
            names=index.get(normalized(p['name']),[])
            if len(names)!=1:
                continue
            row=con.execute('SELECT data FROM careers WHERE player=?',(names[0]['id'],)).fetchone()
            if row:
                try:
                    p['profile']=profile(json.loads(row[0]),p['year'])
                except (ValueError,KeyError,TypeError):
                    pass
    return players


def sync(players,path):
    from nba_api.stats.static import players as static
    from nba_api.stats.endpoints.playercareerstats import PlayerCareerStats
    index={}
    for p in static.get_players():
        index.setdefault(normalized(p['full_name']),[]).append(p)
    out=json.loads(json.dumps(players))
    messages=[];network_failures=0
    cache=Path(path)
    cache.parent.mkdir(parents=True,exist_ok=True)
    with _lock,closing(sqlite3.connect(cache,timeout=30)) as con:
        con.execute('CREATE TABLE IF NOT EXISTS careers(player INTEGER PRIMARY KEY,data TEXT NOT NULL)')
        for p in out:
            matches=index.get(normalized(p['name']),[])
            if len(matches)!=1:
                messages.append(p['name']+': no exact NBA player match; baseline unchanged.')
                continue
            try:
                season(p['year'])
            except ValueError as exc:
                messages.append(p['name']+': '+str(exc));continue
            pid=matches[0]['id']
            cached=con.execute('SELECT data FROM careers WHERE player=?',(pid,)).fetchone()
            try:
                if cached:
                    rows=json.loads(cached[0])
                else:
                    if network_failures>=2:
                        messages.append(p['name']+': NBA source unavailable; baseline unchanged.');continue
                    time.sleep(.6)
                    try:
                        result=PlayerCareerStats(player_id=pid,per_mode36='PerGame',timeout=8).get_normalized_dict()
                        rows=result['SeasonTotalsRegularSeason']
                        con.execute('INSERT INTO careers VALUES(?,?)',(pid,json.dumps(rows)))
                        con.commit()
                    except Exception:
                        network_failures+=1
                        messages.append(p['name']+': NBA request failed; baseline unchanged.');continue
                p['profile']=profile(rows,p['year'])
                messages.append(p['name']+': loaded '+season(p['year'])+'.')
            except (ValueError,KeyError,TypeError) as exc:
                messages.append(p['name']+': '+str(exc))
    return out,messages
