"""Sharp PNG cards rendered directly at full resolution; no GIFs or browser."""
from io import BytesIO
from pathlib import Path
from functools import lru_cache
from PIL import Image, ImageDraw, ImageFont

BG='#0b111c'
PANEL='#152132'
TEXT='#f4f7fc'
MUTED='#9eafc8'
ACCENT='#46dfb0'
BLUE='#71b7ff'


@lru_cache(maxsize=48)
def font(size,bold=False):
    candidates=(['C:/Windows/Fonts/segoeuib.ttf','/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'] if bold else
                ['C:/Windows/Fonts/segoeui.ttf','/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'])
    for path in candidates:
        if Path(path).exists():
            return ImageFont.truetype(path,size)
    return ImageFont.load_default(size=size)


def canvas(height):
    image=Image.new('RGB',(1500,height),BG)
    draw=ImageDraw.Draw(image)
    draw.rectangle((0,0,1500,8),fill=ACCENT)
    return image,draw


def text(draw,xy,value,size=26,color=TEXT,bold=False):
    draw.text(xy,str(value),font=font(size,bold),fill=color)


def fit(draw,value,width,size=30):
    while size>14 and draw.textlength(value,font=font(size,True))>width:
        size-=1
    return size


def png(image):
    out=BytesIO()
    image.save(out,format='PNG',optimize=True)
    return out.getvalue()


def logo(image,raw,xy,size=110):
    if raw:
        try:
            mark=Image.open(BytesIO(raw)).convert('RGBA')
            mark.thumbnail((size,size))
            image.paste(mark,(xy[0]+(size-mark.width)//2,xy[1]+(size-mark.height)//2),mark)
        except (OSError,ValueError):
            pass


def footer(draw,y,note='SIMULATED GAME - not historical NBA results'):
    draw.line((55,y-18,1445,y-18),fill='#29374b',width=2)
    text(draw,(55,y),note,20,MUTED)


def estimated_count(series):
    return sum(p['profile'].get('source')=='estimated positional baseline' for team in (series['high'],series['low']) for p in team['roster'])


def game_card(series,game,logos=(None,None)):
    image,d=canvas(1050)
    number=game['number']
    text(d,(55,38),('TEST  /  ' if series.get('is_test') else '')+f'ATD PLAYOFFS  /  SERIES {series["id"]}  /  GAME {number}',24,MUTED,True)
    text(d,(1220,38),'FINAL / OT' if game['ot'] else 'FINAL',27,ACCENT,True)
    teams=[series['high'],series['low']]
    for i,team in enumerate(teams):
        x=55+i*730
        logo(image,logos[i],(x,110))
        text(d,(x+130,115),f'#{team["seed"]} SEED',22,MUTED)
        text(d,(x+130,156),team['name'],fit(d,team['name'],550,34),TEXT,True)
        text(d,(x,255),game['scores'][i],126,ACCENT if game['winner']==i else TEXT,True)
        text(d,(x+280,300),'HOME' if game['home']==i else 'AWAY',25,BLUE,True)
    wins=game['series_wins']
    if game['series_complete']:
        lead=f'{teams[game["winner"]]["name"]} wins the series {max(wins)}-{min(wins)}'
    elif wins[0]==wins[1]:
        lead=f'Series tied {wins[0]}-{wins[1]}'
    else:
        lead=f'{teams[0 if wins[0]>wins[1] else 1]["name"]} leads {max(wins)}-{min(wins)}'
    text(d,(55,440),lead,fit(d,lead,1360,35),TEXT,True)
    d.rounded_rectangle((55,515,1445,640),radius=18,fill=PANEL)
    labels=['Q1','Q2','Q3','Q4']+(['OT'] if game['ot'] else [])
    for i,team in enumerate(teams):
        text(d,(78,540+i*42),team['name'],fit(d,team['name'],460,24),TEXT,True)
    for j,label in enumerate(labels):
        x=650+j*145
        text(d,(x,478),label,21,MUTED)
        for i in (0,1):
            text(d,(x,540+i*42),game['quarters'][i][j],26,TEXT,True)
    star=game['star']
    text(d,(55,690),'PLAYER OF THE GAME',23,ACCENT,True)
    text(d,(55,733),star['name']+('  '+star['year'] if star['year'] else ''),42,TEXT,True)
    text(d,(55,800),f'{star["pts"]} PTS     {star["reb"]} REB     {star["ast"]} AST',36,BLUE,True)
    story=teams[game['winner']]['name']+' '+game['story']+'.'
    text(d,(55,878),story,fit(d,story,1380,28),MUTED)
    footer(d,985,f'SIMULATED  |  {estimated_count(series)}/20 estimated baselines  |  Higher-seed chance {game["odds_high"]/100:g}%  |  Saved result')
    return png(image)


COLS=[('MIN',660),('PTS',738),('REB',810),('AST',882),('STL',954),('BLK',1026),('TO',1098),('FG',1180),('3PT',1290),('FT',1400)]


def box_card(series,game,side):
    team=series['high'] if side==0 else series['low']
    image,d=canvas(1170)
    text(d,(55,38),('TEST  /  ' if series.get('is_test') else '')+f'GAME {game["number"]}  /  SERIES {series["id"]}  /  BOX SCORE',25,MUTED,True)
    text(d,(55,105),team['name'],fit(d,team['name'],1150,50),TEXT,True)
    text(d,(1280,95),game['scores'][side],74,ACCENT,True)
    text(d,(55,215),'PLAYER / SEASON',22,MUTED,True)
    for label,x in COLS:
        d.text((x,215),label,font=font(19,True),fill=MUTED,anchor='rt')
    rows=game['boxes'][side]
    for i,p in enumerate(rows):
        y=265+i*65
        if i%2==0:
            d.rounded_rectangle((45,y-7,1455,y+52),radius=7,fill=PANEL)
        label=f'{p["name"]}'+(f'  {p["year"]}' if p['year'] else '')
        text(d,(60,y),label,fit(d,label,555,25),TEXT,i<5)
        vals=[p[k] for k in ('min','pts','reb','ast','stl','blk','tov')]+[f'{p[a]}-{p[b]}' for a,b in [('fgm','fga'),('tpm','tpa'),('ftm','fta')]]
        for (_,x),value in zip(COLS,vals):
            d.text((x,y),str(value),font=font(23),fill=ACCENT if x==738 else TEXT,anchor='rt')
    totals={k:sum(p[k] for p in rows) for k in rows[0] if isinstance(rows[0][k],int)}
    y=950
    text(d,(60,y),'TEAM TOTALS',25,ACCENT,True)
    vals=[totals[k] for k in ('min','pts','reb','ast','stl','blk','tov')]+[f'{totals[a]}-{totals[b]}' for a,b in [('fgm','fga'),('tpm','tpa'),('ftm','fta')]]
    for (_,x),value in zip(COLS,vals):
        d.text((x,y),str(value),font=font(23,True),fill=TEXT,anchor='rt')
    text(d,(55,1010),'First five rows: starters. All figures are simulated.',23,MUTED)
    footer(d,1100,f'SIMULATED BOX SCORE  |  {estimated_count(series)}/20 estimated baselines  |  Voting odds decide the winner')
    return png(image)


def averages(series,games):
    result=[]
    for side in (0,1):
        rows=[]
        for p in (series['high'] if side==0 else series['low'])['roster']:
            appearances=[row for g in games for row in g['boxes'][side] if row['name'].strip().casefold()==p['name'].strip().casefold()]
            values={k:sum(row[k] for row in appearances)/len(games) for k in ('pts','reb','ast','stl','blk','min')}
            years={row['year'] for row in appearances}
            rows.append(dict(name=p['name'],year=next(iter(years)) if len(years)==1 else 'Various',**values))
        result.append(rows)
    return result


def stats_card(series,games):
    if not games:
        raise ValueError('Play Game 1 before viewing series averages.')
    image,d=canvas(1450)
    text(d,(55,35),('TEST  /  ' if series.get('is_test') else '')+f'SERIES {series["id"]}  /  {len(games)} GAMES PLAYED',26,MUTED,True)
    data=averages(series,games)
    for side in (0,1):
        top=100+side*610
        team=series['high'] if side==0 else series['low']
        text(d,(55,top),team['name'],40,ACCENT,True)
        for label,x in [('MIN',850),('PTS',980),('REB',1110),('AST',1240),('STL',1350),('BLK',1450)]:
            d.text((x,top+62),label,font=font(20,True),fill=MUTED,anchor='rt')
        for i,p in enumerate(data[side]):
            y=top+105+i*44
            if i%2==0:
                d.rectangle((45,y-3,1460,y+37),fill=PANEL)
            label=p['name']+('  '+p['year'] if p['year'] else '')
            text(d,(60,y),label,fit(d,label,690,24),TEXT,i<5)
            for key,x in [('min',850),('pts',980),('reb',1110),('ast',1240),('stl',1350),('blk',1450)]:
                d.text((x,y),f'{p[key]:.1f}',font=font(23),fill=TEXT,anchor='rt')
    footer(d,1390,'SIMULATED SERIES AVERAGES  |  Saved games only')
    return png(image)
