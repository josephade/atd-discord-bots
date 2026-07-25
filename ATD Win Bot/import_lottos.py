"""
import_lottos.py  —  Bulk-import lotto drafter assignments into lottos.json

Usage:
    1. Save all your lotto pastes into lotto_data.txt
    2. Run:  python import_lottos.py lotto_data.txt

Supported line formats (detected automatically per draft):
  Normal:     :emoji: - @Drafter1  @Drafter2
  Backwards:  @Drafter1 @Drafter2 - :emoji:
  No-sep:     :emoji: @Drafter  OR  @Drafter :emoji:
  ATD 93:     :emoji: : @Drafter
  ATD 87:     Name1/Name2 - FlagEmoji  (Unicode flag emojis, no @ signs)
"""

import re
import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from emoji_map import EMOJI_TEAM_MAP, UNICODE_EMOJI_MAP
from aliases import DRAFTER_ALIASES


def _name_variants(name: str) -> list:
    name = name.strip()
    variants = [name]
    m = re.match(r'^(.*?)\((.+)\)\s*$', name)
    if m:
        base, alias = m.group(1).strip(), m.group(2).strip()
        if base:
            variants.append(base)
        if alias:
            variants.append(alias)
    return variants


def resolve_name(raw: str) -> str:
    q = raw.strip().lower()
    if q in DRAFTER_ALIASES:
        return DRAFTER_ALIASES[q]
    for v in _name_variants(raw.strip()):
        if v.strip().lower() in DRAFTER_ALIASES:
            return DRAFTER_ALIASES[v.strip().lower()]
    return raw.strip()


def parse_drafters(text: str) -> list:
    """Parse @-prefixed names out of text."""
    names = []
    for chunk in text.split('@')[1:]:
        chunk = re.sub(r'\s*\([^)]*\)', '', chunk)
        chunk = re.sub(r'(?<=\S)\s+#\d+.*$', '', chunk)
        chunk = re.sub(r'\s{2,}[-–—].*$', '', chunk)   # strip "   - Active Skip" notes
        chunk = re.sub(r'[\s/]+$', '', chunk)
        name = chunk.strip()
        if name:
            names.append(resolve_name(name))
    return [n for n in names if n]


def parse_plain_names(text: str) -> list:
    """Parse plain names (no @ signs) separated by '/'. Used for ATD 87."""
    raw = [n.strip() for n in text.split('/')]
    return [resolve_name(n) for n in raw if n and n.upper() != 'TBD']


def extract_emoji_names(line: str) -> list:
    """Extract Discord custom emoji names from a line."""
    names = re.findall(r'<a?:([^:]+):\d+>', line)
    if not names:
        names = re.findall(r':([^:\s]+):', line)
    return names


def extract_unicode_flags(line: str) -> list:
    """Extract Unicode country flag emojis (pairs of regional indicators)."""
    return re.findall(r'[\U0001F1E0-\U0001F1FF]{2}', line)


def parse_line(line: str):
    """
    Parse a lotto line.
    Returns (team_identifiers, drafter_names, is_unicode) or (None, None, False) to skip.
    team_identifiers: list of emoji names (str) or flag chars
    is_unicode: True if team_identifiers are Unicode flags
    """
    discord_emojis = extract_emoji_names(line)

    if discord_emojis:
        sep = re.search(r'\s[–—\-]\s', line)

        if sep:
            before = line[:sep.start()]
            after  = line[sep.end():]
            before_emojis = extract_emoji_names(before)
            after_emojis  = extract_emoji_names(after)

            if before_emojis:
                # Normal:    :emoji: - @drafters  OR  :emoji: - Name
                drafters_text = after
            elif after_emojis:
                # Backwards: @drafters - :emoji:  OR  Name - :emoji:
                drafters_text = before
            else:
                drafters_text = after
        else:
            stripped = line.lstrip()
            if stripped.startswith('@'):
                # No-sep backwards: @drafter :emoji:
                drafters_text = re.sub(r'<a?:[^:]+:\d+>', '', line)
                drafters_text = re.sub(r':[^:\s]+:', '', drafters_text)
            else:
                # No-sep normal (ATD 93): :emoji: @drafters OR :emoji: : @drafters
                last_end = 0
                for en in discord_emojis:
                    for pat in [rf'<a?:{re.escape(en)}:\d+>', rf':{re.escape(en)}:']:
                        m = re.search(pat, line)
                        if m:
                            last_end = max(last_end, m.end())
                drafters_text = line[last_end:].lstrip(' :')

        # Use @-split if any @ present, otherwise treat as plain names
        if '@' in drafters_text:
            drafter_names = parse_drafters(drafters_text)
        else:
            drafter_names = parse_plain_names(drafters_text)
        return discord_emojis, drafter_names, False

    # Unicode flag line (ATD 87 style)
    flags = extract_unicode_flags(line)
    if flags:
        sep = re.search(r'\s[–—\-]\s', line)
        if sep:
            names_text = line[:sep.start()].strip()
            if not names_text or names_text.upper() == 'TBD':
                return None, None, False
            drafter_names = parse_plain_names(names_text)
            return flags, drafter_names, True

    return None, None, False


def _merge_drafters(entry: dict, names: list):
    existing_lower = {d.lower() for d in entry['drafters']}
    for n in names:
        if n and n.lower() not in existing_lower:
            entry['drafters'].append(n)
            existing_lower.add(n.lower())


def parse_lotto_text(text: str):
    result  = {}
    skipped = {}
    current_draft = None
    current_team  = None
    draft_counts  = {}   # base ATD key → how many times seen (for D2 tracking)

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        # ── Strip leading number prefixes like "1)", "2.", "21.", "9.." ──────
        line = re.sub(r'^\d+[.)]+\s*', '', line)
        if not line:
            continue

        # ── Draft header ──────────────────────────────────────────────────────
        hdr = re.match(r'^(ATD\s+\d+)', line, re.IGNORECASE)
        if hdr and not line.startswith(':') and not line.startswith('@'):
            base_key = hdr.group(1).strip()
            count = draft_counts.get(base_key, 0) + 1
            draft_counts[base_key] = count
            current_draft = base_key if count == 1 else f"{base_key}-D{count}"
            result.setdefault(current_draft, {})
            current_team = None
            continue

        if current_draft is None:
            continue

        # ── Continuation line: starts with @ and no emoji at all ──────────────
        discord_emojis = extract_emoji_names(line)
        unicode_flags  = extract_unicode_flags(line)
        if not discord_emojis and not unicode_flags and line.startswith('@'):
            if current_team and current_team in result[current_draft]:
                names = parse_drafters(line)
                _merge_drafters(result[current_draft][current_team], names)
            continue

        # ── TBD or blank drafters line ────────────────────────────────────────
        if line.upper().startswith('TBD'):
            continue

        emojis, drafter_names, is_unicode = parse_line(line)
        if emojis is None:
            continue

        # Resolve team name
        team_name = None
        if is_unicode:
            for flag in emojis:
                tn = UNICODE_EMOJI_MAP.get(flag)
                if tn:
                    team_name = tn
                    break
            if not team_name:
                skipped.setdefault(current_draft, set()).add(
                    ''.join(emojis) + ' (unicode flag)'
                )
        else:
            for en in emojis:
                tn = EMOJI_TEAM_MAP.get(en)
                if tn:
                    team_name = tn
                    break
                else:
                    skipped.setdefault(current_draft, set()).add(en)

        if not team_name:
            continue

        if team_name not in result[current_draft]:
            result[current_draft][team_name] = {'drafters': [], 'players': []}
        _merge_drafters(result[current_draft][team_name], drafter_names)
        current_team = team_name

    return result, skipped


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: python import_lottos.py lotto_data.txt")
        sys.exit(1)

    with open(sys.argv[1], 'r', encoding='utf-8') as f:
        text = f.read()

    lottos, skipped = parse_lotto_text(text)

    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'lottos.json')
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(lottos, f, indent=2, ensure_ascii=False)

    total_teams = sum(len(t) for t in lottos.values())
    print(f"OK  lottos.json written -- {len(lottos)} drafts, {total_teams} teams")
    print(f"    {out_path}")

    if skipped:
        print()
        print("WARN  Unrecognised emojis (add to emoji_map.py and re-run):")
        for draft, emojis in sorted(skipped.items()):
            print(f"    {draft}: {', '.join(sorted(emojis))}")
    else:
        print("OK  All emojis recognised")
