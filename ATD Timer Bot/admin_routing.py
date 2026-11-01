"""Private control channel maps to a real draft channel, never a second draft."""
import json
import os
from pathlib import Path

PRIVATE_CHANNEL_ID = 1533242104304631969
DEFAULT_DRAFT_CHANNEL_ID = 934052208624930848
OWNER_ID = 681833601830354944
CONFIG_FILE = Path(os.getenv('STATE_DIR', Path(__file__).parent))/'admin_channel.json'


def target(channel_id):
    if channel_id != PRIVATE_CHANNEL_ID:
        return channel_id
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return DEFAULT_DRAFT_CHANNEL_ID
    value = data.get('draft_channel_id')
    if not isinstance(value, int) or value <= 0 or value == PRIVATE_CHANNEL_ID:
        raise ValueError('Private draft target is invalid. Use !timeradminchannel #draft-channel.')
    return value


def configure(channel_id):
    if channel_id <= 0 or channel_id == PRIVATE_CHANNEL_ID:
        raise ValueError('Choose a real selection channel, not the private control channel.')
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = CONFIG_FILE.with_suffix('.tmp')
    temp.write_text(json.dumps({'draft_channel_id': channel_id}), encoding='utf-8')
    temp.replace(CONFIG_FILE)
