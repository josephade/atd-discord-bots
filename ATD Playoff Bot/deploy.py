"""Deploy from local .env without printing secrets or placing them in the image.

One-time app + volume creation are explicit in README.md. This script refuses
an absent token/volume, stages secrets over stdin, then deploys one machine.
"""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tomllib
import base64

from dotenv import dotenv_values

ROOT=Path(__file__).resolve().parent


def main():
    fly=shutil.which('fly')
    if not fly:
        raise SystemExit('Install the Fly CLI and run fly auth login first.')
    config=tomllib.loads((ROOT/'fly.toml').read_text(encoding='utf-8'))
    app=config['app']
    values=dotenv_values(ROOT/'.env')
    token=(values.get('DISCORD_TOKEN') or '').strip()
    if not token:
        raise SystemExit('Set the NEW bot token in .env first. No app, volume or secrets were changed.')
    result=subprocess.run([fly,'volumes','list','--app',app,'--json'],capture_output=True,text=True,cwd=ROOT)
    if result.returncode:
        raise SystemExit(f'Cannot inspect {app}. Create the app and log into Fly; see README.md. No secrets were uploaded.')
    volumes=json.loads(result.stdout)
    if not any(v.get('name')=='atd_playoff_data' and v.get('region')==config['primary_region'] for v in volumes):
        raise SystemExit('Create atd_playoff_data in the configured region before deploying. See README.md.')
    names=('DISCORD_TOKEN','DISCORD_GUILD_ID','OWNER_ID','PRIVATE_CHANNEL_ID','BACKUP_ADMIN_CHANNEL_ID','COMMISH_ROLE_ID','VOTER_ROLE_ID','PUBLIC_CHANNEL_IDS')
    secrets={name:values.get(name) or '' for name in names}
    if values.get('PLAYOFF_GM_REGISTRY_B64'):
        secrets['PLAYOFF_GM_REGISTRY_B64']=values['PLAYOFF_GM_REGISTRY_B64']
    raw=values.get('GOOGLE_SERVICE_ACCOUNT_JSON')
    credentials=ROOT/(values.get('SERVICE_ACCOUNT_FILE') or 'service_account.json')
    if raw:
        json.loads(raw)
        secrets['GOOGLE_SERVICE_ACCOUNT_B64']=base64.b64encode(raw.encode()).decode()
    elif credentials.is_file():
        raw=credentials.read_text(encoding='utf-8')
        json.loads(raw)
        secrets['GOOGLE_SERVICE_ACCOUNT_B64']=base64.b64encode(raw.encode()).decode()
    # JSON quoted strings are valid double-quoted dotenv values; subprocess
    # stdin keeps credentials out of shell history and process argument lists.
    body='\n'.join(name+'='+json.dumps(value) for name,value in secrets.items())+'\n'
    subprocess.run([fly,'secrets','import','--stage','--app',app],input=body,text=True,cwd=ROOT,check=True)
    subprocess.run([fly,'deploy','--remote-only','--yes','--app',app],cwd=ROOT,check=True)


if __name__=='__main__':
    main()
