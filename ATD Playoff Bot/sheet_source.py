"""Read-only import of the existing Team Sheet's ten roster slots."""
import re
import json
import os
import base64

import engine


def source(text):
    parts = [p.strip() for p in text.split('|',1)]
    if len(parts)!=2 or not parts[1]:
        raise ValueError('Use !simsheet <spreadsheet URL or ID> | <worksheet name>.')
    match=re.search(r'/spreadsheets/d/([A-Za-z0-9_-]+)',parts[0])
    key=match.group(1) if match else parts[0]
    if not re.fullmatch(r'[A-Za-z0-9_-]{20,150}',key):
        raise ValueError('Enter a valid Google spreadsheet URL or ID.')
    return dict(sheet=key,tab=parts[1])


def parse_roster(rows,team):
    matches=[(r,c) for r,row in enumerate(rows) for c,value in enumerate(row)
             if str(value).strip().strip('[]').casefold()==team.strip().strip('[]').casefold()]
    if len(matches)!=1:
        raise ValueError('The team name must match exactly one header on the worksheet. Check !simteam.')
    r,c=matches[0]
    result=[]
    for i in range(10):
        row=rows[r+i+1] if r+i+1<len(rows) else []
        name=str(row[c]).strip() if c<len(row) else ''
        year=str(row[c+1]).strip() if c+1<len(row) else ''
        result.append(dict(name=name,year=year,position=engine.POSITIONS[i]))
    return engine.validate_roster(result)


def client(credentials):
    import gspread
    scopes=['https://www.googleapis.com/auth/spreadsheets.readonly']
    encoded=os.getenv('GOOGLE_SERVICE_ACCOUNT_B64')
    secret=base64.b64decode(encoded).decode('utf-8') if encoded else os.getenv('GOOGLE_SERVICE_ACCOUNT_JSON')
    client=(gspread.service_account_from_dict(json.loads(secret),scopes=scopes) if secret else
            gspread.service_account(filename=credentials,scopes=scopes))
    client.set_timeout(25)
    return client


def load_rows(config,credentials):
    client_instance=client(credentials)
    return client_instance.open_by_key(config['sheet']).worksheet(config['tab']).get_all_values()


def load(config,team,credentials):
    rows=load_rows(config,credentials)
    return parse_roster(rows,team)
