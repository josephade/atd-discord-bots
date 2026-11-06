"""SQLite persistence. A simulated game and its series advance commit together."""
import json
import copy
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import engine

OPENING = ((1,16),(8,9),(4,13),(5,12),(2,15),(7,10),(3,14),(6,11))


def now():
    return datetime.now(timezone.utc).isoformat()


def unpack(row):
    if row is None:
        raise ValueError('Not found. Check the season, seed or series ID.')
    return json.loads(row['data'])


class Store:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True,exist_ok=True)
        with self.connection() as c:
            c.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS seasons(id INTEGER PRIMARY KEY,guild INTEGER NOT NULL,active INTEGER NOT NULL,data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS teams(season INTEGER NOT NULL,seed INTEGER NOT NULL,data TEXT NOT NULL,PRIMARY KEY(season,seed));
                CREATE TABLE IF NOT EXISTS series(id INTEGER PRIMARY KEY,season INTEGER NOT NULL,stage INTEGER NOT NULL,slot INTEGER NOT NULL,data TEXT NOT NULL,UNIQUE(season,stage,slot));
                CREATE TABLE IF NOT EXISTS games(series INTEGER NOT NULL,number INTEGER NOT NULL,data TEXT NOT NULL,PRIMARY KEY(series,number));
                CREATE TABLE IF NOT EXISTS votes(series INTEGER NOT NULL,user INTEGER NOT NULL,choice INTEGER NOT NULL,PRIMARY KEY(series,user));
                CREATE TABLE IF NOT EXISTS focus(guild INTEGER NOT NULL,channel INTEGER NOT NULL,series INTEGER NOT NULL,PRIMARY KEY(guild,channel));
                CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY,created TEXT NOT NULL,actor INTEGER NOT NULL,action TEXT NOT NULL,data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS draft_sources(atd INTEGER PRIMARY KEY,data TEXT NOT NULL);
            ''')

    @contextmanager
    def connection(self, write=False):
        c = sqlite3.connect(self.path,timeout=30)
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA foreign_keys=ON')
        c.execute('PRAGMA busy_timeout=30000')
        try:
            if write:
                c.execute('BEGIN IMMEDIATE')
            yield c
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    def log(self,c,actor,action,data):
        c.execute('INSERT INTO audit(created,actor,action,data) VALUES(?,?,?,?)',(now(),actor,action,json.dumps(data)))

    def create(self,guild,name,actor,is_test=False):
        if not name.strip() or len(name)>80:
            raise ValueError('Use !simcreate ATD 114 (name up to 80 characters).')
        with self.connection(True) as c:
            active=c.execute('SELECT * FROM seasons WHERE guild=? AND active=1',(guild,)).fetchone()
            if active:
                raise ValueError('A season is already active. Finish it and use !simarchive before creating another.')
            data=dict(guild=guild,name=name.strip(),created=now(),source=None,is_test=bool(is_test))
            cur=c.execute('INSERT INTO seasons(guild,active,data) VALUES(?,1,?)',(guild,json.dumps(data)))
            data['id']=cur.lastrowid
            c.execute('UPDATE seasons SET data=? WHERE id=?',(json.dumps(data),data['id']))
            self.log(c,actor,'create',data)
            return data

    def season(self,guild):
        with self.connection() as c:
            row=c.execute('SELECT * FROM seasons WHERE guild=? AND active=1',(guild,)).fetchone()
            if not row:
                raise ValueError('No active playoffs. Ask an admin to run !simcreate <ATD name>.')
            return unpack(row)

    def save_source(self,guild,source,actor):
        season=self.season(guild)
        with self.connection(True) as c:
            season['source']=source
            c.execute('UPDATE seasons SET data=? WHERE id=?',(json.dumps(season),season['id']))
            self.log(c,actor,'sheet_source',dict(season=season['id'],**source))

    def teams(self,season):
        with self.connection() as c:
            return [unpack(r) for r in c.execute('SELECT * FROM teams WHERE season=? ORDER BY seed',(season,))]

    def save_team(self,guild,seed,logo,name,actor,roster=None,expected_roster=None):
        if seed not in range(1,17) or not name.strip() or len(name)>65 or len(logo)>100:
            raise ValueError('Use !simteam <seed 1-16> <emoji> <team name>.')
        season=self.season(guild)
        with self.connection(True) as c:
            if c.execute('SELECT 1 FROM series WHERE season=?',(season['id'],)).fetchone():
                raise ValueError('Teams and rosters lock when the bracket is created. Finish setup before !simbracket.')
            old=c.execute('SELECT * FROM teams WHERE season=? AND seed=?',(season['id'],seed)).fetchone()
            if expected_roster is not None and (not old or unpack(old)['roster']!=expected_roster):
                raise ValueError('The roster changed while loading. Nothing was overwritten; run the command again.')
            data=dict(seed=seed,logo=logo,name=name.strip(),roster=roster if roster is not None else (unpack(old)['roster'] if old else []))
            for team in self.teams(season['id']):
                if team['seed']!=seed and (team['name'].casefold()==name.casefold() or team['logo']==logo):
                    raise ValueError('Each seed needs a distinct team name and logo.')
            if roster is not None:
                engine.validate_roster(data['roster'])
            c.execute('INSERT INTO teams VALUES(?,?,?) ON CONFLICT(season,seed) DO UPDATE SET data=excluded.data',
                      (season['id'],seed,json.dumps(data)))
            self.log(c,actor,'team',dict(season=season['id'],**data))
            return data

    def team(self,guild,seed):
        season=self.season(guild)
        with self.connection() as c:
            return unpack(c.execute('SELECT * FROM teams WHERE season=? AND seed=?',(season['id'],seed)).fetchone())

    def _insert_series(self,c,season,stage,slot,a,b):
        high,low=sorted((a,b),key=lambda t:t['seed'])
        s=dict(season=season['id'],guild=season['guild'],is_test=season.get('is_test',False),stage=stage,slot=slot,high=high,low=low,
               base=None,odds_source=None,hca=1000,penalty_high=0,penalty_low=0,votes_open=True,wins=[0,0],complete=False,created=now())
        cur=c.execute('INSERT INTO series(season,stage,slot,data) VALUES(?,?,?,?)',(season['id'],stage,slot,json.dumps(s)))
        s['id']=cur.lastrowid
        self._save_series(c,s)
        return s

    def _save_series(self,c,s):
        c.execute('UPDATE series SET data=? WHERE id=?',(json.dumps(s),s['id']))

    def draft_source(self,atd):
        with self.connection() as c:
            row=c.execute('SELECT data FROM draft_sources WHERE atd=?',(atd,)).fetchone()
            return unpack(row) if row else None

    def install_setup(self,guild,atd,teams,source,actor,still_current=None):
        """Install all validated seeds/rosters/bracket together, never partial setup."""
        if sorted(t['seed'] for t in teams)!=list(range(1,17)):
            raise ValueError('The standings must supply 16 distinct playoff seeds.')
        names=[]
        for t in teams:
            engine.validate_roster(t['roster'])
            names.extend(p['name'].casefold() for p in t['roster'])
        if len(set(names))!=160 or len({t['logo'] for t in teams})!=16 or len({t['name'].casefold() for t in teams})!=16:
            raise ValueError('Duplicate teams, logos or players were found. No setup was saved.')
        with self.connection(True) as c:
            if still_current is not None and not still_current():
                raise ValueError('This setup was cancelled. No tournament was installed.')
            row=c.execute('SELECT * FROM seasons WHERE guild=? AND active=1',(guild,)).fetchone()
            if row:
                season=unpack(row)
                numbers=re.findall(r'\d+',season['name'])
                if season.get('atd',int(numbers[-1]) if numbers else None)!=atd:
                    raise ValueError('Another ATD is active. Finish/archive it before starting this one.')
                if c.execute('SELECT 1 FROM series WHERE season=?',(season['id'],)).fetchone():
                    return season,False
            else:
                season=dict(name=f'ATD {atd}',guild=guild,created=now(),is_test=False)
                cur=c.execute('INSERT INTO seasons(guild,active,data) VALUES(?,1,?)',(guild,json.dumps(season)))
                season['id']=cur.lastrowid
            season.update(atd=atd,source=source)
            c.execute('UPDATE seasons SET data=? WHERE id=?',(json.dumps(season),season['id']))
            c.execute('DELETE FROM teams WHERE season=?',(season['id'],))
            for t in teams:
                c.execute('INSERT INTO teams VALUES(?,?,?)',(season['id'],t['seed'],json.dumps(t)))
            by_seed={t['seed']:t for t in teams}
            for slot,(a,b) in enumerate(OPENING):
                self._insert_series(c,season,1,slot,by_seed[a],by_seed[b])
            c.execute('INSERT INTO draft_sources VALUES(?,?) ON CONFLICT(atd) DO UPDATE SET data=excluded.data',(atd,json.dumps(source)))
            self.log(c,actor,'automatic_setup',dict(season=season['id'],atd=atd,teams=16,source=source))
            return season,True

    def refresh_lineups(self,guild,sid,rosters,expected_games,source,actor):
        """Fresh lineup snapshot for the next game; historical games stay immutable."""
        with self.connection(True) as c:
            s=self.series(guild,sid)
            season=unpack(c.execute('SELECT * FROM seasons WHERE id=? AND active=1',(s['season'],)).fetchone())
            if season.get('source')!=source or sum(s['wins'])!=expected_games:
                raise ValueError('The source or series changed while loading lineups. Refresh the panel and try again.')
            if s['complete']:
                raise ValueError('This series has finished.')
            if len(rosters)!=2:
                raise ValueError('Both lineups are required. No game was played.')
            for key,roster in zip(('high','low'),rosters):
                engine.validate_roster(roster)
                eligible={p['name'].strip().casefold() for p in s[key]['roster']}
                if {p['name'].strip().casefold() for p in roster}!=eligible:
                    raise ValueError(f"{s[key]['name']}: the sheet changed the player roster, not just the lineup. Check the sheet before playing; no game was simulated.")
                s[key]['roster']=roster
            s['lineup_read_at']=now()
            self._save_series(c,s)
            self.log(c,actor,'lineups_refreshed',dict(series=sid,next_game=expected_games+1,source=source))
            return s

    def bracket(self,guild,actor):
        season=self.season(guild)
        with self.connection(True) as c:
            if c.execute('SELECT 1 FROM series WHERE season=?',(season['id'],)).fetchone():
                raise ValueError('The bracket already exists. Use !playoffs to view it.')
            teams={t['seed']:t for t in self.teams(season['id'])}
            if set(teams)!=set(range(1,17)):
                raise ValueError('Register all 16 seeds with !simteam first.')
            names=set()
            for t in teams.values():
                engine.validate_roster(t['roster'])
                for p in t['roster']:
                    key=p['name'].casefold()
                    if key in names:
                        raise ValueError(f"{p['name']} appears on more than one playoff team. Correct the rosters first.")
                    names.add(key)
            result=[self._insert_series(c,season,1,i,teams[a],teams[b]) for i,(a,b) in enumerate(OPENING)]
            self.log(c,actor,'bracket',dict(season=season['id']))
            return result

    def series(self,guild,sid):
        with self.connection() as c:
            s=unpack(c.execute('SELECT * FROM series WHERE id=?',(sid,)).fetchone())
            if s['guild']!=guild:
                raise ValueError('That series belongs to another server.')
            return s

    def all_series(self,guild):
        season=self.season(guild)
        with self.connection() as c:
            return [unpack(r) for r in c.execute('SELECT * FROM series WHERE season=? ORDER BY stage,slot',(season['id'],))]

    def focus(self,guild,channel,sid=None):
        with self.connection(sid is not None) as c:
            if sid is not None:
                self.series(guild,sid)
                c.execute('INSERT INTO focus VALUES(?,?,?) ON CONFLICT(guild,channel) DO UPDATE SET series=excluded.series',(guild,channel,sid))
                return sid
            row=c.execute('SELECT series FROM focus WHERE guild=? AND channel=?',(guild,channel)).fetchone()
            if not row:
                raise ValueError('Open !sim in this channel/thread and select a series, or include the series ID in the command.')
            return row[0]

    def configure(self,guild,sid,actor,**updates):
        with self.connection(True) as c:
            s=self.series(guild,sid)
            self._editable(c,s)
            s.update(updates)
            self._save_series(c,s)
            self.log(c,actor,'configure',dict(series=sid,**updates))
            return s

    def team_gms(self,guild,seed):
        from memberships import registered
        season=self.season(guild)
        return registered(season,self.team(guild,seed))

    def can_play(self,guild,sid,user):
        s=self.series(guild,sid)
        try:
            if self.season(guild)['id']!=s['season']:
                return False
            return any(user in self.team_gms(guild,t['seed']) for t in (s['high'],s['low']))
        except ValueError:
            return False

    def gm_series(self,guild,user):
        rows=self.all_series(guild)
        stage=max((s['stage'] for s in rows),default=0)
        return [s for s in rows if s['stage']==stage and self.can_play(guild,s['id'],user)]

    def set_series_gms(self,guild,sid,high,low,actor):
        if set(high)&set(low):
            raise ValueError('A GM cannot represent both teams in a series.')
        with self.connection(True) as c:
            s=self.series(guild,sid)
            season=self.season(guild)
            if season['id']!=s['season']:
                raise ValueError('This tournament is closed.')
            if any(not isinstance(uid,int) or uid<=0 for uid in high+low):
                raise ValueError('Invalid Discord user ID.')
            for team,ids in zip((s['high'],s['low']),(high,low)):
                current=self.team(guild,team['seed'])
                current['gm_ids']=sorted(set(ids))
                c.execute('UPDATE teams SET data=? WHERE season=? AND seed=?',(json.dumps(current),s['season'],team['seed']))
            self.log(c,actor,'team_gms',dict(series=sid,high=high,low=low))

    def _editable(self,c,s):
        if not c.execute('SELECT 1 FROM seasons WHERE id=? AND active=1',(s['season'],)).fetchone():
            raise ValueError('This tournament is closed. Saved results remain available.')
        if c.execute('SELECT 1 FROM games WHERE series=?',(s['id'],)).fetchone():
            raise ValueError('This series has started. Odds, penalties and ballots are locked.')

    def vote(self,guild,sid,user,choice):
        if choice not in range(8):
            raise ValueError('Vote 0-7: 0/1/2/3 = higher seed in 4/5/6/7; 4/5/6/7 = lower seed in 7/6/5/4.')
        with self.connection(True) as c:
            s=self.series(guild,sid)
            self._editable(c,s)
            if not s['votes_open']:
                raise ValueError('Voting is closed for this series.')
            c.execute('INSERT INTO votes VALUES(?,?,?) ON CONFLICT(series,user) DO UPDATE SET choice=excluded.choice',(sid,user,choice))

    def close_votes(self,guild,sid,actor):
        with self.connection(True) as c:
            s=self.series(guild,sid)
            self._editable(c,s)
            if not s['votes_open']:
                raise ValueError('Voting is already closed. Use !simodds for a pre-game correction.')
            choices=[r[0] for r in c.execute('SELECT choice FROM votes WHERE series=?',(sid,))]
            s.update(base=engine.vote_odds(choices),votes_open=False,odds_source=f'{len(choices)} ballots: expected game wins / total expected games')
            self._save_series(c,s)
            self.log(c,actor,'close_votes',dict(series=sid,base=s['base'],ballots=len(choices)))
            return s

    def reset_bracket(self,guild,actor):
        season=self.season(guild)
        with self.connection(True) as c:
            ids=[r[0] for r in c.execute('SELECT id FROM series WHERE season=?',(season['id'],))]
            if any(c.execute('SELECT 1 FROM games WHERE series=?',(sid,)).fetchone() for sid in ids):
                raise ValueError('A game has already been played. Reset is blocked: saved results cannot be deleted or rerolled.')
            for sid in ids:
                c.execute('DELETE FROM votes WHERE series=?',(sid,))
                c.execute('DELETE FROM focus WHERE series=?',(sid,))
            c.execute('DELETE FROM series WHERE season=?',(season['id'],))
            self.log(c,actor,'reset_unplayed_bracket',dict(season=season['id']))

    def game(self,guild,sid,number,actor=None):
        with self.connection(actor is not None) as c:
            s=self.series(guild,sid)
            row=c.execute('SELECT * FROM games WHERE series=? AND number=?',(sid,number)).fetchone()
            if row:
                return s,unpack(row),False
            if actor is None:
                raise ValueError('That game has not been played yet.')
            season=unpack(c.execute('SELECT * FROM seasons WHERE id=? AND active=1',(s['season'],)).fetchone())
            if s['complete']:
                raise ValueError('This best-of-seven series is finished.')
            if number!=sum(s['wins'])+1 or number not in range(1,8):
                raise ValueError(f"Play Game {sum(s['wins'])+1} next. Games must be simulated in order.")
            if s['base'] is None:
                raise ValueError('Set official voting odds with !simodds, or close ballots with !simclosevotes first.')
            seed=secrets.token_hex(32)
            g=engine.simulate(s,number,seed)
            s['wins'][g['winner']]+=1
            s['votes_open']=False
            s['complete']=max(s['wins'])==4
            g.update(created=now(),actor=actor,series_wins=s['wins'].copy(),series_complete=s['complete'],
                     rosters=copy.deepcopy([s['high']['roster'],s['low']['roster']]),lineup_read_at=s.get('lineup_read_at'))
            c.execute('INSERT INTO games VALUES(?,?,?)',(sid,number,json.dumps(g)))
            self._save_series(c,s)
            self.log(c,actor,'game',dict(series=sid,number=number,seed=seed,roll=g['roll'],odds_high=g['odds_high']))
            return s,g,True

    def games(self,guild,sid):
        self.series(guild,sid)
        with self.connection() as c:
            return [unpack(r) for r in c.execute('SELECT * FROM games WHERE series=? ORDER BY number',(sid,))]

    def advance(self,guild,actor):
        season=self.season(guild)
        with self.connection(True) as c:
            rows=[unpack(r) for r in c.execute('SELECT * FROM series WHERE season=? ORDER BY stage,slot',(season['id'],))]
            if not rows:
                raise ValueError('Create the bracket first with !simbracket.')
            stage=max(s['stage'] for s in rows)
            current=[s for s in rows if s['stage']==stage]
            if not all(s['complete'] for s in current):
                raise ValueError('Finish every series in this round before advancing.')
            if stage==4:
                raise ValueError('The Finals are finished. Your champion is shown in !playoffs.')
            winners=[s['high'] if s['wins'][0]==4 else s['low'] for s in current]
            result=[self._insert_series(c,season,stage+1,i//2,winners[i],winners[i+1]) for i in range(0,len(winners),2)]
            self.log(c,actor,'advance',dict(season=season['id'],stage=stage+1))
            return result

    def cancel(self,guild,atd,actor):
        """Close only the explicitly named active ATD; never delete results."""
        with self.connection(True) as c:
            row=c.execute('SELECT * FROM seasons WHERE guild=? AND active=1',(guild,)).fetchone()
            if not row:
                raise ValueError('No tournament is active. Nothing was cancelled.')
            season=unpack(row)
            numbers=re.findall(r'\d+',season['name'])
            number=season.get('atd',int(numbers[-1]) if numbers else None)
            if number!=atd:
                raise ValueError(f"The active tournament is {season['name']}, not ATD {atd}. Nothing was cancelled.")
            season.update(cancelled=True,cancelled_at=now(),cancelled_by=actor)
            c.execute('UPDATE seasons SET active=0,data=? WHERE id=?',(json.dumps(season),season['id']))
            c.execute('DELETE FROM focus WHERE guild=?',(guild,))
            self.log(c,actor,'cancel',dict(season=season['id'],atd=atd))
            return season

    def archive(self,guild,actor):
        season=self.season(guild)
        with self.connection(True) as c:
            rows=[unpack(r) for r in c.execute('SELECT * FROM series WHERE season=?',(season['id'],))]
            if rows and not season.get('is_test') and not any(s['stage']==4 and s['complete'] for s in rows):
                raise ValueError('Finish the Finals before archiving. No results were removed.')
            c.execute('UPDATE seasons SET active=0 WHERE id=?',(season['id'],))
            c.execute('DELETE FROM focus WHERE guild=?',(guild,))
            self.log(c,actor,'archive',dict(season=season['id']))

    def export(self,guild):
        with self.connection() as c:
            seasons=[dict(r) for r in c.execute('SELECT * FROM seasons WHERE guild=?',(guild,))]
            ids=[s['id'] for s in seasons]
            data={'seasons':[json.loads(s['data']) for s in seasons],'teams':[],'series':[],'games':[],'votes':[]}
            for season in ids:
                data['teams'] += [dict(r) for r in c.execute('SELECT * FROM teams WHERE season=?',(season,))]
                for row in c.execute('SELECT * FROM series WHERE season=?',(season,)):
                    data['series'].append(dict(row))
                    data['games'] += [dict(r) for r in c.execute('SELECT * FROM games WHERE series=?',(row['id'],))]
                    data['votes'] += [dict(r) for r in c.execute('SELECT * FROM votes WHERE series=?',(row['id'],))]
            data['audit']=[dict(r) for r in c.execute('SELECT * FROM audit') if self._audit_belongs(r,guild,ids,data['series'])]
            return data

    @staticmethod
    def _audit_belongs(row,guild,seasons,series):
        d=json.loads(row['data'])
        return d.get('guild')==guild or d.get('season') in seasons or d.get('series') in {s['id'] for s in series}
