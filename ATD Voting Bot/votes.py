"""Parse original ballots only; preserve the voter's tiers without scoring."""
import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass


def normalize(value):
    return ' '.join(unicodedata.normalize('NFKC', value).strip().casefold().split())


@dataclass
class Ballot:
    key: str
    timestamp: str
    name: str
    teams: list
    error: str = ''


def parse_rows(rows, name_column='Enter Discord Name', require_name=True):
    if not rows:
        raise ValueError('The response sheet is empty.')
    header = rows[0]
    names = [i for i, h in enumerate(header) if normalize(h) == normalize(name_column)]
    times = [i for i, h in enumerate(header) if normalize(h) in {'timestamp', 'tidsmerke'}]
    columns = [(i, m.group(1).strip()) for i, h in enumerate(header)
               if (m := re.search(r'\[([^\]]+)\]\s*$', h.strip()))]
    if len(times) != 1 or not columns or (require_name and len(names) != 1):
        raise ValueError('Expected Timestamp, Enter Discord Name, and team headers ending in [Team Name].')
    if len({normalize(t) for _, t in columns}) != len(columns):
        raise ValueError('Duplicate team headers.')
    ballots = []
    for row in rows[1:]:
        def cell(i):
            return row[i].strip() if i < len(row) else ''
        timestamp = cell(times[0])
        # A repeated response header starts the manually copied/formula table.
        # Its rows may retain timestamps and integer ranks that resemble tiers.
        # Only the first response table belongs to the form.
        if normalize(timestamp) in {'timestamp', 'tidsmerke'}:
            break
        # Ignore copied rankings, repeated headers, and summary tables below responses.
        if not re.match(r'^\d{1,4}[./-]\d{1,2}[./-]\d{1,4}\s', timestamp):
            continue
        name = cell(names[0]) if names else ''
        teams, errors = [], []
        for i, team in columns:
            value = cell(i)
            match = re.fullmatch(r'(?:Tier\s*)?(\d+)', value, re.I)
            if not match or not 1 <= int(match.group(1)) <= 50:
                errors.append(f'{team}: missing or invalid tier')
            else:
                teams.append((team, int(match.group(1))))
        if require_name and not name:
            errors.append('Discord name is blank')
        # Exclude derived columns; editing a ballot creates a new delivery revision.
        payload = [timestamp, name, [(t, cell(i)) for i, t in columns]]
        key = hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()
        ballots.append(Ballot(key, timestamp, name, teams, '; '.join(errors)))
    return ballots


def resolve_member(name, members, aliases):
    query = normalize(name)
    alias_id = aliases.get(query)
    if alias_id is not None:
        return next((m for m in members if m.id == alias_id and not m.bot), None)
    mention = re.fullmatch(r'<@!?(\d+)>|(\d{15,22})', name.strip())
    if mention:
        uid = int(mention.group(1) or mention.group(2))
        return next((m for m in members if m.id == uid and not m.bot), None)
    # Exact matches only. Any collision across usernames/nicknames needs an alias.
    matches = {m.id: m for m in members if not m.bot and query and query in
               {normalize(x) for x in (m.name, m.display_name, m.global_name or '', str(m))}}
    return next(iter(matches.values())) if len(matches) == 1 else None
