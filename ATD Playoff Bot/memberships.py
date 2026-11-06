"""Private, explicitly verified team ownership; never infer identities from names."""
import base64
import json
import os
import re


def compact(value):
    return re.sub('[^a-z0-9]','',str(value).casefold())


def atd_number(season):
    values=re.findall(r'\d+',season['name'])
    return season.get('atd',int(values[-1]) if values else None)


def registered(season,team):
    if 'gm_ids' in team:
        return team['gm_ids']
    encoded=os.getenv('PLAYOFF_GM_REGISTRY_B64')
    if not encoded:
        return []
    registry=json.loads(base64.b64decode(encoded))
    entries=registry.get(str(atd_number(season)),{})
    return next(([int(uid) for uid in ids] for name,ids in entries.items() if compact(name)==compact(team['name'])),[])


def parse(text):
    if not text.strip():
        return []
    result=[]
    for token in re.split(r'[,\s]+',text.strip()):
        match=re.fullmatch(r'(?:<@!?)?(\d{15,22})>?',token)
        if not match:
            raise ValueError('Use Discord user IDs or actual mentions, separated by commas. Leave blank to disable GM simulation for that team.')
        result.append(int(match[1]))
    if len(set(result))>10:
        raise ValueError('Enter at most 10 GMs per team.')
    return sorted(set(result))
