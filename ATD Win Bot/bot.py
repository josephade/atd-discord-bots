import discord
from discord.ext import commands
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import asyncio
import json
import os
import re
import difflib
import time
from collections import Counter
from config import DISCORD_TOKEN, DISCORD_GUILD_ID, SPREADSHEET_ID, ROSTER_SHEET_ID, STATE_DIR, SERVICE_ACCOUNT_FILE
from emoji_map import EMOJI_TEAM_MAP
from adp import ADP_MAP
from champions import CHAMPIONS
from champion_teams import CHAMPION_TEAMS
from aliases import DRAFTER_ALIASES

SCOPE         = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
WIN_SHEET_TAB = 'Win Sheet'
MIN_PCT_GAMES = 300
RECENT_COUNT  = 5

C_GOLD    = 0xFFD700
C_TEAL    = 0x1ABC9C
C_BLUE    = 0x3498DB
C_ORANGE  = 0xFF8C00
C_PURPLE  = 0x9B59B6
C_RED     = 0xE74C3C
C_GREEN   = 0x2ECC71
C_GRAY    = 0x95A5A6
C_PROFILE = 0xD4AF37

MEDALS        = ['🥇', '🥈', '🥉']
PROFILES_FILE = os.path.join(STATE_DIR, 'profiles.json')
LOTTOS_FILE   = os.path.join(STATE_DIR, 'lottos.json')

# ── Profile storage ───────────────────────────────────────────────────────────

def _load_profiles() -> dict:
    try:
        with open(PROFILES_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def _save_profiles(data: dict):
    os.makedirs(os.path.dirname(PROFILES_FILE), exist_ok=True)
    with open(PROFILES_FILE, 'w') as f:
        json.dump(data, f, indent=2)

def _profile_by_name(profiles: dict, name: str):
    q = name.strip().lower()
    for uid, p in profiles.items():
        if p.get('sheet_name', '').lower() == q:
            return uid, p
    for uid, p in profiles.items():
        if q in p.get('sheet_name', '').lower():
            return uid, p
    return None, None

# ── Lotto storage ─────────────────────────────────────────────────────────────

def _load_lottos() -> dict:
    try:
        with open(LOTTOS_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def _save_lottos(data: dict):
    os.makedirs(os.path.dirname(LOTTOS_FILE), exist_ok=True)
    with open(LOTTOS_FILE, 'w') as f:
        json.dump(data, f, indent=2)

def _parse_lotto_drafters(drafters_part: str) -> list:
    names = []
    for chunk in drafters_part.split('@')[1:]:
        chunk = re.sub(r'\s*\([^)]*\)', '', chunk)
        chunk = re.sub(r'(?<=\S)\s+#\d+.*$', '', chunk)
        chunk = re.sub(r'[\s/]+$', '', chunk)
        name = chunk.strip()
        if name:
            names.append(name)
    return names

def _team_to_emoji_name(team_name: str) -> str:
    q = team_name.strip().lower()
    best = None
    for emoji_name, mapped in EMOJI_TEAM_MAP.items():
        if mapped.strip().lower() == q:
            if best is None or (len(emoji_name) < len(best) and '~' not in emoji_name and ':' not in emoji_name):
                best = emoji_name
    return best

def _name_variants(name: str) -> list:
    """Return all name variants from a Win Sheet name like 'Liam(Hakeem Lowry)'."""
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

def _resolve_drafter_name(name: str, profiles: dict) -> str:
    """Map a raw lotto name to the canonical Win Sheet name via aliases or profile variants."""
    q = name.strip().lower()
    if q in DRAFTER_ALIASES:
        return DRAFTER_ALIASES[q]
    for p in profiles.values():
        sheet_name = p.get('sheet_name', '')
        if not sheet_name:
            continue
        if any(v.strip().lower() == q for v in _name_variants(sheet_name)):
            return sheet_name
    return name

def _all_name_variants(name: str) -> list:
    """All variants of a name: parenthetical splits + alias forward/reverse lookups."""
    variants = _name_variants(name)
    name_lower = name.strip().lower()
    # Forward: query is itself an alias → add its canonical target
    if name_lower in DRAFTER_ALIASES:
        target = DRAFTER_ALIASES[name_lower]
        if target not in variants:
            variants.append(target)
    # Reverse: find all aliases that point to this name (or its canonical target)
    targets_lower = {v.lower() for v in variants}
    for old, canonical in DRAFTER_ALIASES.items():
        if canonical.strip().lower() in targets_lower and old not in [v.lower() for v in variants]:
            variants.append(old)
    return variants

def _drafter_match(query: str, drafters: list) -> bool:
    variants = [v.lower() for v in _all_name_variants(query)]
    return any(v == d.strip().lower() for v in variants for d in drafters)

def _get_drafter_players(sheet_name: str, lottos: dict) -> dict:
    counts = {}
    for teams in lottos.values():
        for entry in teams.values():
            if _drafter_match(sheet_name, entry.get('drafters', [])):
                for p in entry.get('players', []):
                    n = p['name']
                    counts[n] = counts.get(n, 0) + 1
    return counts

# ── Roster sheet helpers ──────────────────────────────────────────────────────

def _fetch_draft_tab(draft_num: str):
    creds  = ServiceAccountCredentials.from_json_keyfile_name(SERVICE_ACCOUNT_FILE, SCOPE)
    client = gspread.authorize(creds)
    sh     = client.open_by_key(ROSTER_SHEET_ID)
    target = f"ATD {draft_num}".upper()
    for ws in sh.worksheets():
        if ws.title.upper().startswith(target):
            return ws.title, ws.get_all_values()
    return None, None

def _fetch_all_roster_tabs(needed_nums: set):
    """Fetch roster tabs for needed draft numbers.
    One shared connection; individual get_all_values() with 1s delay to stay under quota."""
    creds  = ServiceAccountCredentials.from_json_keyfile_name(SERVICE_ACCOUNT_FILE, SCOPE)
    client = gspread.authorize(creds)
    sh     = client.open_by_key(ROSTER_SHEET_ID)
    all_ws = sh.worksheets()
    # Map draft_num -> first matching worksheet (D1 tab wins over D2)
    ws_map = {}
    for ws in all_ws:
        m = re.match(r'ATD\s*(\d+)', ws.title, re.IGNORECASE)
        if m:
            dn = m.group(1)
            if dn in needed_nums and dn not in ws_map:
                ws_map[dn] = ws
    if not ws_map:
        return {}
    print(f"[refreshrosters] Fetching {len(ws_map)} tabs: {sorted(ws_map.keys(), key=int)}")
    tabs = {}
    ws_list = list(ws_map.items())
    for i, (dn, ws) in enumerate(ws_list):
        tabs[dn] = ws.get_all_values()
        if i < len(ws_list) - 1:
            time.sleep(1.1)  # 60 reads/min limit → ~1 per second
    return tabs

def _resolve_emoji(raw: str) -> str:
    m = re.match(r'<a?:([^:]+):\d+>', raw.strip())
    if m:
        key = m.group(1)
        if key in EMOJI_TEAM_MAP:
            return EMOJI_TEAM_MAP[key]
        for k, v in EMOJI_TEAM_MAP.items():
            if k.lower() == key.lower():
                return v
    m2 = re.match(r':([^:]+):', raw.strip())
    if m2:
        key = m2.group(1)
        if key in EMOJI_TEAM_MAP:
            return EMOJI_TEAM_MAP[key]
        for k, v in EMOJI_TEAM_MAP.items():
            if k.lower() == key.lower():
                return v
    return raw.strip()

def _find_team_roster(tab_data: list, team_query: str):
    if not tab_data:
        return None, []
    q = team_query.strip().lower()
    for row_idx, row in enumerate(tab_data):
        for col in range(len(row)):
            cell      = row[col].strip() if len(row) > col else ''
            year_cell = row[col + 1].strip() if len(row) > col + 1 else ''
            if year_cell.lower() != 'year' or not cell:
                continue
            if cell.lower() == q or q in cell.lower() or cell.lower() in q:
                players = []
                for data_row in tab_data[row_idx + 1:]:
                    player = data_row[col].strip() if len(data_row) > col else ''
                    yr     = data_row[col + 1].strip() if len(data_row) > col + 1 else ''
                    if not player or yr.lower() == 'year':
                        break
                    players.append({'name': player, 'year': yr})
                return cell, players
    return None, []

# ── Win sheet helpers ─────────────────────────────────────────────────────────

def _fetch_raw():
    creds  = ServiceAccountCredentials.from_json_keyfile_name(SERVICE_ACCOUNT_FILE, SCOPE)
    client = gspread.authorize(creds)
    sh     = client.open_by_key(SPREADSHEET_ID)
    return sh.worksheet(WIN_SHEET_TAB).get_all_values()

async def _get_raw():
    return await asyncio.get_event_loop().run_in_executor(None, _fetch_raw)

# ── Parsing ───────────────────────────────────────────────────────────────────

def _safe_int(s):
    try:
        return int(str(s).strip())
    except (ValueError, TypeError):
        return None

def _safe_pct(s):
    try:
        return float(str(s).strip().replace('%', ''))
    except (ValueError, TypeError):
        return None

def _parse(raw):
    if not raw:
        return [], [], []

    headers    = raw[0]
    draft_col  = {}
    for i, h in enumerate(headers):
        h = h.strip()
        if h.startswith('Draft '):
            try:
                draft_col[int(h.replace('Draft ', '').strip())] = i
            except ValueError:
                pass

    draft_numbers = sorted(draft_col)
    recent_drafts = draft_numbers[-RECENT_COUNT:]

    h_lower   = [h.strip().lower() for h in headers]
    total_idx = next((i for i, h in enumerate(h_lower) if h == 'total'), None)
    pct_idx   = next((i for i, h in enumerate(h_lower) if h == '%'), None)
    over_idx  = next((i for i, h in enumerate(h_lower) if 'wins over' in h), None)
    max_idx   = next((i for i, h in enumerate(h_lower) if h == 'max w'), None)
    min_idx   = next((i for i, h in enumerate(h_lower) if h == 'min w'), None)

    drafters = []
    for row in raw[1:]:
        name = row[0].strip() if row else ''
        if not name:
            continue
        total = _safe_int(row[total_idx] if total_idx is not None and len(row) > total_idx else '')
        pct   = _safe_pct(row[pct_idx]   if pct_idx   is not None and len(row) > pct_idx   else '')
        over  = _safe_int(row[over_idx]  if over_idx  is not None and len(row) > over_idx  else '')
        max_w = _safe_int(row[max_idx]   if max_idx   is not None and len(row) > max_idx   else '')
        min_w = _safe_int(row[min_idx]   if min_idx   is not None and len(row) > min_idx   else '')
        draft_wins = {}
        for num, col in draft_col.items():
            v = _safe_int(row[col] if len(row) > col else '')
            if v is not None:
                draft_wins[num] = v
        drafters.append({
            'name':        name,
            'total':       total or 0,
            'pct':         pct,
            'over':        over,
            'max':         max_w,
            'min':         min_w,
            'played':      len(draft_wins),
            'draft_wins':  draft_wins,
            'recent_wins': sum(draft_wins.get(d, 0) for d in recent_drafts),
        })

    return drafters, draft_numbers, recent_drafts

def _find(drafters, query):
    q        = query.strip().lower()
    resolved = DRAFTER_ALIASES.get(q, q).strip().lower()

    # Build variant sets
    canon_variants = {v.strip().lower() for v in _name_variants(DRAFTER_ALIASES.get(q, query))}
    query_variants = {v.strip().lower() for v in _name_variants(query)}
    all_variants   = canon_variants | query_variants

    # Exact match — canonical name first so aliases always win over stale Win Sheet rows
    for d in drafters:
        if d['name'].strip().lower() == resolved:
            return d
    for d in drafters:
        if d['name'].strip().lower() in all_variants:
            return d
    # Partial match
    for d in drafters:
        dn = d['name'].strip().lower()
        if resolved in dn or dn in resolved:
            return d
    for d in drafters:
        dn = d['name'].strip().lower()
        if any(v in dn for v in all_variants if len(v) >= 3):
            return d
    return None

# ── Format helpers ─────────────────────────────────────────────────────────────

def _pct_str(pct):
    return f"{pct:.2f}%" if pct is not None else 'N/A'

def _over_str(over):
    if over is None:
        return 'N/A'
    return f"+{over}" if over >= 0 else str(over)

def _err(msg):
    return discord.Embed(description=msg, color=C_RED)

def _champ_match(query: str, candidate: str) -> bool:
    return query.strip().lower() == candidate.strip().lower()

def _get_champ_record(name: str) -> tuple:
    variants = _all_name_variants(name)
    wins, runner_ups = [], []
    for draft_num, data in CHAMPIONS.items():
        if any(_champ_match(v, w) for v in variants for w in data.get('w', [])):
            wins.append(draft_num)
        if any(_champ_match(v, ru) for v in variants for ru in data.get('ru', [])):
            runner_ups.append(draft_num)
    return sorted(wins), sorted(runner_ups)

def _title_player(name: str) -> str:
    result = name.title()
    result = re.sub(r"'([A-Z])(\s|$)", lambda m: "'" + m.group(1).lower() + m.group(2), result)
    result = re.sub(r'\bMc([a-z])', lambda m: 'Mc' + m.group(1).upper(), result)
    result = re.sub(r'\bMac([a-z])', lambda m: 'Mac' + m.group(1).upper(), result)
    return result

def _lookup_adp_player(raw: str):
    """Return (canonical_key, display_name) if found in ADP_MAP, else (None, None)."""
    key = raw.strip().lower()
    if key in ADP_MAP:
        return key, _title_player(key)
    norm = re.sub(r"['.]+", '', key).strip()
    for k in ADP_MAP:
        if re.sub(r"['.]+", '', k).strip() == norm:
            return k, _title_player(k)
    return None, None

def _get_adp(player_name: str) -> float:
    key = player_name.strip().lower()
    if key in ADP_MAP:
        return ADP_MAP[key]
    norm = re.sub(r"['.]+", '', key).strip()
    for k, v in ADP_MAP.items():
        if re.sub(r"['.]+", '', k).strip() == norm:
            return v
    return 9999.0

class PlayerListView(discord.ui.View):
    def __init__(self, title: str, lines: list, subtitle: str = '', per_page: int = 10):
        super().__init__(timeout=120)
        self.title       = title
        self.subtitle    = subtitle
        self.lines       = lines
        self.per_page    = per_page
        self.page        = 0
        self.total_pages = max(1, (len(lines) + per_page - 1) // per_page)
        self._update_buttons()

    def _update_buttons(self):
        self.children[0].disabled = self.page == 0
        self.children[1].disabled = self.page >= self.total_pages - 1

    def get_embed(self) -> discord.Embed:
        start = self.page * self.per_page
        block = '\n'.join(self.lines[start:start + self.per_page])
        desc  = (f"{self.subtitle}\n" if self.subtitle else '') + f"```\n{block}\n```"
        embed = discord.Embed(title=self.title, description=desc, color=C_PROFILE)
        embed.set_footer(text=f"Page {self.page + 1} of {self.total_pages}  ·  {len(self.lines)} players total")
        return embed

    @discord.ui.button(emoji='◀', style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page -= 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.get_embed(), view=self)

    @discord.ui.button(emoji='▶', style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page += 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.get_embed(), view=self)

# ── Bot ───────────────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents, help_command=None)

@bot.event
async def on_ready():
    print(f"✅ ATD Win Bot online as {bot.user}")
    guild = bot.get_guild(DISCORD_GUILD_ID)
    print(f"📊 Active in server: {guild.name}" if guild else f"⚠️  Guild {DISCORD_GUILD_ID} not found")

def _in_channel(ctx):
    return ctx.guild is not None and ctx.guild.id == DISCORD_GUILD_ID

# ── !standings ────────────────────────────────────────────────────────────────

@bot.command(name='standings')
async def cmd_standings(ctx, mode: str = '', count: str = ''):
    if not _in_channel(ctx):
        return

    async with ctx.typing():
        raw = await _get_raw()
    drafters, draft_numbers, _ = _parse(raw)

    if not drafters:
        await ctx.send(embed=_err("⚠️ Could not load standings data."))
        return

    mode = mode.lower()

    if mode == 'pct':
        pool     = sorted([d for d in drafters if d['total'] >= MIN_PCT_GAMES and d['pct'] is not None],
                          key=lambda d: d['pct'], reverse=True)
        title    = "📊 ATD Standings — Win %"
        footer   = f"Min {MIN_PCT_GAMES} wins to qualify · {len(pool)} qualified drafters"
        color    = C_TEAL
        medal_fn = lambda d: f"{d['total']}W · {_pct_str(d['pct'])} · {_over_str(d['over'])}"
        row_fn   = lambda i, d: f"{i:>2}. {d['name']:<22}  {d['total']:>4}W  {_pct_str(d['pct']):>7}  {_over_str(d['over']):>6}"
    elif mode == 'recent':
        n = RECENT_COUNT
        if count:
            try:
                n = int(count)
                if n <= 0:
                    raise ValueError
            except ValueError:
                await ctx.send(embed=_err(f"❌ `{count}` is not a valid number of drafts."))
                return

        window = draft_numbers[-n:] if draft_numbers else []
        if not window:
            await ctx.send(embed=_err("⚠️ No draft data available."))
            return

        def _window_wins(d, _window=window):
            return sum(d['draft_wins'].get(num, 0) for num in _window)

        rd       = f"Drafts {window[0]}–{window[-1]}" if len(window) > 1 else f"Draft {window[0]}"
        pool     = sorted([d for d in drafters if _window_wins(d) > 0],
                          key=_window_wins, reverse=True)
        title    = f"📊 ATD Standings — Recent ({rd})"
        footer   = f"{len(pool)} active drafters in last {n} draft{'s' if n != 1 else ''}"
        color    = C_BLUE
        medal_fn = lambda d: f"{_window_wins(d)}W"
        row_fn   = lambda i, d: f"{i:>2}. {d['name']:<22}  {_window_wins(d):>3}W"
    else:
        pool     = sorted(drafters, key=lambda d: d['total'], reverse=True)
        title    = "📊 ATD All-Time Standings"
        footer   = f"Ranked by total wins · {len(drafters)} drafters"
        color    = C_GOLD
        medal_fn = lambda d: f"{d['total']}W · {_pct_str(d['pct'])} · {_over_str(d['over'])}"
        row_fn   = lambda i, d: f"{i:>2}. {d['name']:<22}  {d['total']:>4}W  {_pct_str(d['pct']):>7}  {_over_str(d['over']):>6}"

    top  = '\n'.join(f"{MEDALS[i]} **{d['name']}** — {medal_fn(d)}" for i, d in enumerate(pool[:3]))
    rest = '\n'.join(row_fn(i, d) for i, d in enumerate(pool[3:20], 4))
    desc = top + (f"\n```\n{rest}\n```" if rest else "")

    embed = discord.Embed(title=title, description=desc, color=color)
    embed.set_footer(text=footer)
    await ctx.send(embed=embed)

# ── !winrate ──────────────────────────────────────────────────────────────────

@bot.command(name='winrate')
async def cmd_winrate(ctx):
    if not _in_channel(ctx):
        return

    MIN_WINRATE_WINS = 350

    async with ctx.typing():
        raw = await _get_raw()
    drafters, _, _ = _parse(raw)

    pool = sorted(
        [d for d in drafters if d['total'] >= MIN_WINRATE_WINS and d['pct'] is not None],
        key=lambda d: d['pct'], reverse=True
    )

    if not pool:
        await ctx.send(embed=_err(f"⚠️ No drafters have reached {MIN_WINRATE_WINS} wins yet."))
        return

    RANK_ICONS = {0: '🥇', 1: '🥈', 2: '🥉'}
    lines = [
        f"{RANK_ICONS.get(i, f'{i+1:>2}.')} {d['name']:<22}  {_pct_str(d['pct']):>7}  {d['total']:>4}W"
        for i, d in enumerate(pool)
    ]

    view = PlayerListView(
        title="📈 ATD Win Rate Leaderboard",
        lines=lines,
        subtitle=f"`Min {MIN_WINRATE_WINS} wins · {len(pool)} of {len(drafters)} qualified`",
        per_page=10,
    )
    await ctx.send(embed=view.get_embed(), view=view)

# ── !record ───────────────────────────────────────────────────────────────────

@bot.command(name='record')
async def cmd_record(ctx, *, name: str = ''):
    if not _in_channel(ctx):
        return
    if not name:
        await ctx.send(embed=_err("Usage: `!record <drafter name>`"))
        return

    async with ctx.typing():
        raw = await _get_raw()
    drafters, _, recent_drafts = _parse(raw)

    d = _find(drafters, name)
    if not d:
        await ctx.send(embed=_err(f"❌ No drafter found matching **{name}**."))
        return

    dw       = d['draft_wins']
    best_num = max(dw, key=dw.get) if dw else None
    wrst_num = min(dw, key=dw.get) if dw else None
    rd_label = f"D{recent_drafts[0]}–{recent_drafts[-1]}"

    embed = discord.Embed(title=f"📋 {d['name']}", color=C_ORANGE)
    embed.add_field(name="Total Wins",          value=f"**{d['total']}W**",                  inline=True)
    embed.add_field(name="Win %",               value=f"**{_pct_str(d['pct'])}**",           inline=True)
    embed.add_field(name="Wins over .500",      value=f"**{_over_str(d['over'])}**",         inline=True)
    embed.add_field(name="Drafts Played",       value=str(d['played']),                       inline=True)
    embed.add_field(name=f"Recent ({rd_label})",value=f"{d['recent_wins']}W",                 inline=True)
    if best_num is not None:
        embed.add_field(name="Best Draft",      value=f"Draft {best_num} — {dw[best_num]}W", inline=True)
    if wrst_num is not None and wrst_num != best_num:
        embed.add_field(name="Worst Draft",     value=f"Draft {wrst_num} — {dw[wrst_num]}W", inline=True)
    await ctx.send(embed=embed)

# ── !season ───────────────────────────────────────────────────────────────────

@bot.command(name='season')
async def cmd_season(ctx, draft_num: str = ''):
    if not _in_channel(ctx):
        return
    if not draft_num:
        await ctx.send(embed=_err("Usage: `!season <draft number>` — e.g. `!season 103`"))
        return

    try:
        num = int(draft_num)
    except ValueError:
        await ctx.send(embed=_err(f"❌ `{draft_num}` is not a valid draft number."))
        return

    async with ctx.typing():
        raw = await _get_raw()
    drafters, draft_numbers, _ = _parse(raw)

    if num not in draft_numbers:
        avail = ', '.join(str(n) for n in draft_numbers)
        await ctx.send(embed=_err(f"❌ Draft {num} not found.\nAvailable: {avail}"))
        return

    participants = sorted(
        [(d['name'], d['draft_wins'][num]) for d in drafters if num in d['draft_wins']],
        key=lambda x: x[1], reverse=True
    )

    if not participants:
        await ctx.send(embed=_err(f"⚠️ No win data recorded for Draft {num} yet."))
        return

    top  = '\n'.join(f"{MEDALS[i]} **{name}** — {wins}W" for i, (name, wins) in enumerate(participants[:3]))
    rest = '\n'.join(f"{i:>2}. {name:<22}  {wins}W" for i, (name, wins) in enumerate(participants[3:], 4))
    desc = top + (f"\n```\n{rest}\n```" if rest else "")

    embed = discord.Embed(title=f"🏆 Draft {num} Results", description=desc, color=C_PURPLE)
    embed.set_footer(text=f"{len(participants)} drafters participated")
    await ctx.send(embed=embed)

# ── !findwin ──────────────────────────────────────────────────────────────────

@bot.command(name='findwin')
async def cmd_findwin(ctx, *, args: str = ''):
    if not _in_channel(ctx):
        return
    if not args:
        await ctx.send(embed=_err("Usage: `!findwin ATD <number> [name]` — e.g. `!findwin ATD 104` or `!findwin ATD 104 Momo`"))
        return

    m = re.match(r'(?i)(?:ATD\s*)?(\d+)\s*(.*)$', args.strip())
    if not m:
        await ctx.send(embed=_err("Usage: `!findwin ATD <number> [name]` — e.g. `!findwin ATD 104` or `!findwin ATD 104 Momo`"))
        return

    draft_num = int(m.group(1))
    name      = m.group(2).strip()

    if not name:
        uid      = str(ctx.author.id)
        profiles = _load_profiles()
        self_p   = profiles.get(uid, {})
        if not self_p.get('sheet_name'):
            await ctx.send(embed=_err("Usage: `!findwin ATD <number> <name>` — or link yourself first with `!linkprofile <name>`"))
            return
        name = self_p['sheet_name']

    async with ctx.typing():
        raw = await _get_raw()
    drafters, draft_numbers, _ = _parse(raw)

    if draft_num not in draft_numbers:
        avail = ', '.join(str(n) for n in draft_numbers)
        await ctx.send(embed=_err(f"❌ Draft {draft_num} not found.\nAvailable: {avail}"))
        return

    d = _find(drafters, name)
    if not d:
        await ctx.send(embed=_err(f"❌ No drafter found matching **{name}**."))
        return

    wins = d['draft_wins'].get(draft_num)
    if wins is None:
        await ctx.send(embed=_err(f"⚠️ **{d['name']}** didn't play in ATD {draft_num}."))
        return

    embed = discord.Embed(
        title=f"🏀  {d['name']} — ATD {draft_num}",
        description=f"**{wins}** win{'s' if wins != 1 else ''}",
        color=C_ORANGE,
    )
    await ctx.send(embed=embed)

# ── !compare ──────────────────────────────────────────────────────────────────

@bot.command(name='compare')
async def cmd_compare(ctx, *, args: str = ''):
    if not _in_channel(ctx):
        return

    if ' vs ' not in args.lower():
        await ctx.send(embed=_err("Usage: `!compare <name1> vs <name2>`"))
        return

    idx   = args.lower().index(' vs ')
    name1 = args[:idx].strip()
    name2 = args[idx + 4:].strip()

    async with ctx.typing():
        raw = await _get_raw()
    drafters, _, _ = _parse(raw)

    d1 = _find(drafters, name1)
    d2 = _find(drafters, name2)
    if not d1:
        await ctx.send(embed=_err(f"❌ No drafter found matching **{name1}**."))
        return
    if not d2:
        await ctx.send(embed=_err(f"❌ No drafter found matching **{name2}**."))
        return

    def best(d):
        return f"{max(d['draft_wins'].values())}W" if d['draft_wins'] else 'N/A'
    def worst(d):
        return f"{min(d['draft_wins'].values())}W" if d['draft_wins'] else 'N/A'

    rows = [
        ("Total Wins",  f"{d1['total']}W",    f"{d2['total']}W"),
        ("Win %",       _pct_str(d1['pct']),  _pct_str(d2['pct'])),
        ("Over .500",   _over_str(d1['over']),_over_str(d2['over'])),
        ("Drafts",      str(d1['played']),     str(d2['played'])),
        ("Best Draft",  best(d1),              best(d2)),
        ("Worst Draft", worst(d1),             worst(d2)),
    ]

    lw     = max(len(r[0]) for r in rows)
    vw     = max(max(len(r[1]), len(r[2])) for r in rows)
    header = f"{'':>{lw}}  {d1['name'][:vw]:>{vw}}  {d2['name'][:vw]:>{vw}}"
    sep    = '─' * len(header)
    body   = '\n'.join(f"{r[0]:<{lw}}  {r[1]:>{vw}}  {r[2]:>{vw}}" for r in rows)

    embed = discord.Embed(
        title=f"⚔️ {d1['name']} vs {d2['name']}",
        description=f"```\n{header}\n{sep}\n{body}\n```",
        color=C_RED
    )
    await ctx.send(embed=embed)

# ── !winstats ─────────────────────────────────────────────────────────────────

@bot.command(name='winstats')
async def cmd_winstats(ctx):
    if not _in_channel(ctx):
        return

    async with ctx.typing():
        raw = await _get_raw()
    drafters, _, recent_drafts = _parse(raw)

    if not drafters:
        await ctx.send(embed=_err("⚠️ Could not load data."))
        return

    by_total  = sorted(drafters, key=lambda d: d['total'], reverse=True)
    qualified = [d for d in drafters if d['total'] >= MIN_PCT_GAMES and d['pct'] is not None]
    by_pct    = sorted(qualified, key=lambda d: d['pct'], reverse=True)
    by_recent = sorted([d for d in drafters if d['recent_wins'] > 0], key=lambda d: d['recent_wins'], reverse=True)
    rd        = f"D{recent_drafts[0]}–{recent_drafts[-1]}"

    embed = discord.Embed(title="📈 ATD League Win Stats", color=C_GREEN)
    embed.add_field(name="👑 Most Wins All-Time",        value=f"**{by_total[0]['name']}**\n{by_total[0]['total']}W",  inline=True)
    if by_pct:
        embed.add_field(name=f"📈 Best Win % (≥{MIN_PCT_GAMES}W)", value=f"**{by_pct[0]['name']}**\n{_pct_str(by_pct[0]['pct'])}", inline=True)
    if by_recent:
        embed.add_field(name=f"🔥 Recent Hot Streak ({rd})",       value=f"**{by_recent[0]['name']}**\n{by_recent[0]['recent_wins']}W", inline=True)
    embed.add_field(name="📉 Fewest Wins All-Time",      value=f"**{by_total[-1]['name']}**\n{by_total[-1]['total']}W", inline=True)
    embed.add_field(name="👥 Total Drafters",            value=str(len(drafters)), inline=True)
    await ctx.send(embed=embed)

# ── !ranks ────────────────────────────────────────────────────────────────────

@bot.command(name='ranks')
async def cmd_rank(ctx, *, name: str = ''):
    if not _in_channel(ctx):
        return
    if not name:
        uid      = str(ctx.author.id)
        profiles = _load_profiles()
        self_p   = profiles.get(uid, {})
        if not self_p.get('sheet_name'):
            await ctx.send(embed=_err("Usage: `!ranks <drafter name>` — or link yourself first with `!linkprofile <name>`"))
            return
        name = self_p['sheet_name']

    async with ctx.typing():
        raw = await _get_raw()
    drafters, _, _ = _parse(raw)

    d = _find(drafters, name)
    if not d:
        await ctx.send(embed=_err(f"❌ No drafter found matching **{name}**."))
        return

    total    = len(drafters)
    by_wins  = sorted(drafters, key=lambda x: x['total'], reverse=True)
    by_pct   = sorted([x for x in drafters if x['pct'] is not None], key=lambda x: x['pct'], reverse=True)
    by_over  = sorted([x for x in drafters if x['over'] is not None], key=lambda x: x['over'], reverse=True)

    rank_wins = next((i + 1 for i, x in enumerate(by_wins) if x['name'] == d['name']), None)
    rank_pct  = next((i + 1 for i, x in enumerate(by_pct)  if x['name'] == d['name']), None)
    rank_over = next((i + 1 for i, x in enumerate(by_over) if x['name'] == d['name']), None)

    def rank_label(r, n):
        if r is None:
            return 'N/A'
        pct = round((1 - (r - 1) / n) * 100)
        return f"**#{r}** of {n}  *(top {pct}%)*"

    embed = discord.Embed(title=f"🏅 {d['name']} — Rankings", color=0x5865F2)
    embed.add_field(name="Total Wins",     value=f"{rank_label(rank_wins, total)}\n{d['total']}W",             inline=False)
    embed.add_field(name="Win %",          value=f"{rank_label(rank_pct, len(by_pct))}\n{_pct_str(d['pct'])}", inline=False)
    embed.add_field(name="Wins over .500", value=f"{rank_label(rank_over, len(by_over))}\n{_over_str(d['over'])}", inline=False)
    await ctx.send(embed=embed)

# ── !historys ─────────────────────────────────────────────────────────────────

@bot.command(name='historys')
async def cmd_history(ctx, *, name: str = ''):
    if not _in_channel(ctx):
        return
    if not name:
        uid      = str(ctx.author.id)
        profiles = _load_profiles()
        self_p   = profiles.get(uid, {})
        if not self_p.get('sheet_name'):
            await ctx.send(embed=_err("Usage: `!historys <drafter name>` — or link yourself first with `!linkprofile <name>`"))
            return
        name = self_p['sheet_name']

    async with ctx.typing():
        raw = await _get_raw()
    drafters, _, _ = _parse(raw)

    d = _find(drafters, name)
    if not d:
        await ctx.send(embed=_err(f"❌ No drafter found matching **{name}**."))
        return

    dw = d['draft_wins']
    if not dw:
        await ctx.send(embed=_err(f"**{d['name']}** has no draft history recorded."))
        return

    played    = sorted(dw.keys())
    best_num  = max(dw, key=dw.get)
    worst_num = min(dw, key=dw.get)

    entries = [f"D{n}: {dw[n]}W{'★' if n == best_num else ('▼' if n == worst_num else '')}" for n in played]
    rows    = [entries[i:i + 4] for i in range(0, len(entries), 4)]
    table   = '\n'.join('  '.join(f"{e:<10}" for e in row) for row in rows)

    embed = discord.Embed(
        title=f"📅 {d['name']} — Career History",
        description=f"```\n{table}\n```",
        color=0xF39C12
    )
    embed.add_field(name="Drafts Played", value=str(len(played)),                           inline=True)
    embed.add_field(name="Best",          value=f"Draft {best_num} ({dw[best_num]}W ★)",    inline=True)
    embed.add_field(name="Worst",         value=f"Draft {worst_num} ({dw[worst_num]}W ▼)",  inline=True)
    await ctx.send(embed=embed)

# ── !above500 ─────────────────────────────────────────────────────────────────

@bot.command(name='above500')
async def cmd_above500(ctx):
    if not _in_channel(ctx):
        return

    async with ctx.typing():
        raw = await _get_raw()
    drafters, _, _ = _parse(raw)

    pool  = sorted([d for d in drafters if d['over'] is not None and d['over'] > 0], key=lambda d: d['over'], reverse=True)
    below = len([d for d in drafters if d['over'] is not None and d['over'] <= 0])

    RANK_ICONS = {0: '🥇', 1: '🥈', 2: '🥉'}
    lines = [
        f"{RANK_ICONS.get(i, f'{i+1:>2}.')} {d['name']:<22}  {_over_str(d['over']):>6}  {_pct_str(d['pct']):>7}"
        for i, d in enumerate(pool)
    ]

    view = PlayerListView(
        title=f"✅ Drafters Above .500 ({len(pool)} of {len(drafters)})",
        lines=lines,
        subtitle=f"`{below} drafter(s) at or below .500`",
        per_page=20,
    )
    view.color = C_GREEN
    await ctx.send(embed=view.get_embed(), view=view)

# ── !below500 ─────────────────────────────────────────────────────────────────

@bot.command(name='below500')
async def cmd_below500(ctx):
    if not _in_channel(ctx):
        return

    async with ctx.typing():
        raw = await _get_raw()
    drafters, _, _ = _parse(raw)

    pool  = sorted([d for d in drafters if d['over'] is not None and d['over'] < 0], key=lambda d: d['over'])
    above = len([d for d in drafters if d['over'] is not None and d['over'] >= 0])

    WORST_ICONS = {0: '🔴', 1: '🟠', 2: '🟡'}
    lines = [
        f"{WORST_ICONS.get(i, f'{i+1:>2}.')} {d['name']:<22}  {_over_str(d['over']):>6}  {_pct_str(d['pct']):>7}"
        for i, d in enumerate(pool)
    ]

    view = PlayerListView(
        title=f"❌ Drafters Below .500 ({len(pool)} of {len(drafters)})",
        lines=lines,
        subtitle=f"`{above} drafter(s) at or above .500`",
        per_page=20,
    )
    await ctx.send(embed=view.get_embed(), view=view)

# ── !drafts ───────────────────────────────────────────────────────────────────

@bot.command(name='drafts')
async def cmd_drafts(ctx):
    if not _in_channel(ctx):
        return

    async with ctx.typing():
        raw = await _get_raw()
    _, draft_numbers, _ = _parse(raw)

    rows  = [draft_numbers[i:i + 6] for i in range(0, len(draft_numbers), 6)]
    table = '\n'.join('  '.join(f"D{n:<4}" for n in row) for row in rows)

    embed = discord.Embed(
        title=f"📋 Available Drafts ({len(draft_numbers)} total)",
        description=f"```\n{table}\n```",
        color=C_GRAY
    )
    embed.set_footer(text=f"Most recent: Draft {draft_numbers[-1]}  ·  Use !season <num> to view results")
    await ctx.send(embed=embed)

# ── !active ───────────────────────────────────────────────────────────────────

@bot.command(name='active')
async def cmd_active(ctx):
    if not _in_channel(ctx):
        return

    async with ctx.typing():
        raw = await _get_raw()
    drafters, _, _ = _parse(raw)

    pool = sorted(drafters, key=lambda d: d['played'], reverse=True)
    top  = '\n'.join(f"{MEDALS[i]} **{d['name']}** — {d['played']} drafts" for i, d in enumerate(pool[:3]))
    rest = '\n'.join(f"{i:>2}. {d['name']:<22}  {d['played']:>2} drafts" for i, d in enumerate(pool[3:20], 4))
    desc = top + (f"\n```\n{rest}\n```" if rest else "")

    embed = discord.Embed(title="🗓️ Most Active Drafters", description=desc, color=0xE67E22)
    embed.set_footer(text=f"{len(drafters)} total drafters")
    await ctx.send(embed=embed)

# ── !linkprofile ──────────────────────────────────────────────────────────────

@bot.command(name='linkprofile')
async def cmd_linkprofile(ctx, *, name: str = ''):
    if not _in_channel(ctx):
        return
    if not name:
        await ctx.send(embed=_err("Usage: `!linkprofile <your name on the Win Sheet>`"))
        return

    async with ctx.typing():
        raw = await _get_raw()
    drafters, _, _ = _parse(raw)

    d = _find(drafters, name)
    if not d:
        await ctx.send(embed=_err(f"❌ No drafter found matching **{name}** on the Win Sheet."))
        return

    uid      = str(ctx.author.id)
    profiles = _load_profiles()
    existing = profiles.get(uid, {})
    profiles[uid] = {**existing, 'sheet_name': d['name']}
    _save_profiles(profiles)

    embed = discord.Embed(
        title="✅ Profile Linked",
        description=f"Your Discord account is now linked to **{d['name']}** on the Win Sheet.\nUse `!profile` to view your card.",
        color=C_PROFILE
    )
    await ctx.send(embed=embed)

# ── !sigteam ──────────────────────────────────────────────────────────────────

@bot.command(name='sigteam')
async def cmd_sigteam(ctx, *, args: str = ''):
    if not _in_channel(ctx):
        return

    uid      = str(ctx.author.id)
    profiles = _load_profiles()
    if uid not in profiles or 'sheet_name' not in profiles[uid]:
        await ctx.send(embed=_err("❌ You haven't linked your profile yet. Run `!linkprofile <name>` first."))
        return

    m = re.match(r'(?i)ATD\s+(\d+)\s+(.*)', args.strip())
    if not m:
        await ctx.send(embed=_err("Usage: `!sigteam ATD <num> <team emoji>`\nExample: `!sigteam ATD 103 :Mavericks:`"))
        return

    draft_num = m.group(1)
    team_raw  = m.group(2).strip()

    async with ctx.typing():
        loop = asyncio.get_event_loop()
        tab_title, tab_data = await loop.run_in_executor(None, _fetch_draft_tab, draft_num)

    if not tab_data:
        await ctx.send(embed=_err(f"❌ Could not find a draft tab for **ATD {draft_num}**. Check the number and try again."))
        return

    team_name_resolved = _resolve_emoji(team_raw)
    actual_name, players = _find_team_roster(tab_data, team_name_resolved)

    if not actual_name:
        seen, teams_in_tab = set(), []
        for row in tab_data:
            for col in range(len(row)):
                cell      = row[col].strip() if len(row) > col else ''
                year_cell = row[col + 1].strip() if len(row) > col + 1 else ''
                if cell and year_cell.lower() == 'year' and cell.lower() not in seen:
                    teams_in_tab.append(cell)
                    seen.add(cell.lower())
        await ctx.send(embed=_err(
            f"❌ Team **{team_name_resolved}** not found in ATD {draft_num}.\n"
            f"Teams in that draft: {', '.join(teams_in_tab)}"
        ))
        return

    profiles[uid]['sig_team'] = {
        'draft':     f"ATD {draft_num}",
        'tab_title': tab_title,
        'team_name': actual_name,
        'players':   players,
    }
    _save_profiles(profiles)

    roster_lines = '\n'.join(f"{i:>2}. {p['name']:<22} {p['year']}" for i, p in enumerate(players, 1))
    embed = discord.Embed(
        title="🏆 Signature Team Saved",
        description=f"**ATD {draft_num} — {actual_name}**\n```\n{roster_lines}\n```",
        color=C_PROFILE
    )
    embed.set_footer(text=f"This team will appear on your !profile · Tab: {tab_title}")
    await ctx.send(embed=embed)

# ── !favplayer ────────────────────────────────────────────────────────────────

@bot.command(name='favplayer')
async def cmd_favplayer(ctx, *, players: str = ''):
    if not _in_channel(ctx):
        return
    if not players:
        await ctx.send(embed=_err("Usage: `!favplayer <player>`\nExample: `!favplayer LeBron James`"))
        return

    uid      = str(ctx.author.id)
    profiles = _load_profiles()
    if uid not in profiles or 'sheet_name' not in profiles[uid]:
        await ctx.send(embed=_err("❌ You haven't linked your profile yet. Run `!linkprofile <name>` first."))
        return

    cleaned     = players.strip().strip('"').strip("'")
    new_players = [p.strip() for p in cleaned.split(',') if p.strip()]
    existing       = profiles[uid].get('fav_players', [])
    existing_lower = {p.lower() for p in existing}

    added    = []
    rejected = []

    for raw in new_players:
        canonical_key, display_name = _lookup_adp_player(raw)
        if canonical_key:
            if display_name.lower() not in existing_lower:
                existing.append(display_name)
                existing_lower.add(display_name.lower())
                added.append(display_name)
        else:
            key = raw.strip().lower()
            matches = difflib.get_close_matches(key, ADP_MAP.keys(), n=1, cutoff=0.45)
            if not matches and len(key) >= 3:
                prefix = key[:3]
                for k in sorted(ADP_MAP.keys(), key=lambda x: ADP_MAP[x]):
                    if any(w.startswith(prefix) for w in k.split()):
                        matches = [k]
                        break
            if matches:
                suggestion = _title_player(matches[0])
                rejected.append(f"**{raw}** is not a basketball player. Did you mean **{suggestion}**?")
            else:
                rejected.append(f"**{raw}** is not a basketball player.")

    profiles[uid]['fav_players'] = existing
    _save_profiles(profiles)

    lines = []
    if added:
        lines.append("✅ Added: " + ', '.join(f"**{p}**" for p in added))
    lines.extend(rejected)
    if not added and not rejected:
        lines.append("No changes — all players already saved.")

    embed = discord.Embed(title="⭐ Favourite Players", description='\n'.join(lines), color=C_PROFILE)
    embed.set_footer(text=f"{len(existing)} player(s) saved · These will appear on your !profile")
    await ctx.send(embed=embed)

# ── !clearfavplayers ──────────────────────────────────────────────────────────

@bot.command(name='clearfavplayers')
async def cmd_clearfavplayers(ctx):
    if not _in_channel(ctx):
        return
    uid      = str(ctx.author.id)
    profiles = _load_profiles()
    if uid not in profiles or 'sheet_name' not in profiles[uid]:
        await ctx.send(embed=_err("❌ You haven't linked your profile yet. Run `!linkprofile <name>` first."))
        return
    profiles[uid]['fav_players'] = []
    _save_profiles(profiles)
    embed = discord.Embed(title="⭐ Favourite Players Cleared", description="Your favourite players list has been reset.", color=C_PROFILE)
    await ctx.send(embed=embed)

# ── !favemoji ─────────────────────────────────────────────────────────────────

@bot.command(name='favemoji')
async def cmd_favemoji(ctx, *, emoji: str = ''):
    if not _in_channel(ctx):
        return
    if not emoji:
        await ctx.send(embed=_err("Usage: `!favemoji <emoji>`\nExample: `!favemoji 🐐`"))
        return
    uid      = str(ctx.author.id)
    profiles = _load_profiles()
    if uid not in profiles or 'sheet_name' not in profiles[uid]:
        await ctx.send(embed=_err("❌ You haven't linked your profile yet. Run `!linkprofile <name>` first."))
        return

    existing = profiles[uid].get('fav_emoji', [])
    if isinstance(existing, str):
        existing = [existing] if existing else []
    new_emoji = emoji.strip()
    if new_emoji not in existing:
        existing.append(new_emoji)
    profiles[uid]['fav_emoji'] = existing
    _save_profiles(profiles)

    combined = ' '.join(existing)
    embed = discord.Embed(
        title=f"{combined}  Favourite Emoji Saved",
        description=f"{len(existing)} emoji(s) saved · These will appear on your profile.",
        color=C_PROFILE
    )
    await ctx.send(embed=embed)

# ── !clearemoji ───────────────────────────────────────────────────────────────

@bot.command(name='clearemoji')
async def cmd_clearemoji(ctx):
    if not _in_channel(ctx):
        return
    uid      = str(ctx.author.id)
    profiles = _load_profiles()
    if uid not in profiles or 'sheet_name' not in profiles[uid]:
        await ctx.send(embed=_err("❌ You haven't linked your profile yet. Run `!linkprofile <name>` first."))
        return
    profiles[uid]['fav_emoji'] = []
    _save_profiles(profiles)
    embed = discord.Embed(title="Emoji Cleared", description="Your profile emoji have been reset.", color=C_PROFILE)
    await ctx.send(embed=embed)

# ── !setlotto ─────────────────────────────────────────────────────────────────

@bot.command(name='setlotto')
async def cmd_setlotto(ctx, *, args: str = ''):
    if not _in_channel(ctx):
        return
    if not args:
        await ctx.send(embed=_err("Usage: `!setlotto ATD 104`\nthen paste the lotto lines below"))
        return

    try:
        lines = args.strip().split('\n')
        m = re.match(r'(?i)(?:ATD\s*)?(\d+)', lines[0].strip())
        if not m:
            await ctx.send(embed=_err("❌ First line must be the draft number, e.g. `ATD 104`"))
            return

        draft_num = m.group(1)
        draft_key = f"ATD {draft_num}"

        async with ctx.typing():
            loop = asyncio.get_event_loop()
            tab_title, tab_data = await loop.run_in_executor(None, _fetch_draft_tab, draft_num)

        if not tab_data:
            await ctx.send(embed=_err(f"❌ No roster tab found for ATD {draft_num}."))
            return

        lottos      = _load_lottos()
        draft_entry = {}
        skipped     = []

        for line in lines[1:]:
            line = line.strip()
            if not line:
                continue

            # Resolve <@user_id> mentions — prefer linked Win Sheet name, fall back to display name
            resolved_line = line
            profiles_data = _load_profiles()
            for uid_match in re.finditer(r'<@!?(\d+)>', line):
                member_id = int(uid_match.group(1))
                member = ctx.guild.get_member(member_id)
                if not member:
                    try:
                        member = await ctx.guild.fetch_member(member_id)
                    except Exception:
                        pass
                if member:
                    sheet_name = profiles_data.get(str(member_id), {}).get('sheet_name')
                    name_to_use = sheet_name if sheet_name else member.display_name
                    resolved_line = resolved_line.replace(uid_match.group(0), f"@{name_to_use}")

            # Extract emoji names — try <:name:id> first, then plain :name:
            emoji_names = re.findall(r'<a?:([^:]+):\d+>', resolved_line)
            if not emoji_names:
                emoji_names = re.findall(r':([^:\s]+):', resolved_line)
            if not emoji_names:
                continue

            sep_m = re.search(r'\s[–—-]\s', resolved_line)
            if not sep_m:
                continue
            drafters_part = resolved_line[sep_m.end():]
            drafter_names = [_resolve_drafter_name(n, profiles_data) for n in _parse_lotto_drafters(drafters_part)]

            for emoji_name in emoji_names:
                team_name = EMOJI_TEAM_MAP.get(emoji_name)
                if not team_name:
                    for k, v in EMOJI_TEAM_MAP.items():
                        if k.lower() == emoji_name.lower():
                            team_name = v
                            break
                if not team_name:
                    skipped.append(emoji_name)
                    continue

                _, players = _find_team_roster(tab_data, team_name)
                draft_entry[team_name] = {'drafters': drafter_names, 'players': players}

        existing_draft = lottos.get(draft_key, {})
        existing_draft.update(draft_entry)
        lottos[draft_key] = existing_draft
        _save_lottos(lottos)

        desc_lines = [f"**{team}** → {', '.join(e['drafters'])}" for team, e in list(draft_entry.items())[:20]]
        desc = '\n'.join(desc_lines)
        if len(draft_entry) > 20:
            desc += f'\n*...and {len(draft_entry) - 20} more*'
        if skipped:
            desc += f'\n\n⚠️ Unrecognised emojis (skipped): `{"`, `".join(skipped)}`'

        embed = discord.Embed(title=f"✅ Lotto Updated — {draft_key}", description=desc or "No entries parsed.", color=C_GREEN)
        embed.set_footer(text=f"{len(draft_entry)} added/updated · {len(existing_draft)} total teams · Tab: {tab_title}")
        await ctx.send(embed=embed)

    except Exception as e:
        print(f"[setlotto error] {e}")
        await ctx.send(embed=_err(f"⚠️ Error processing lotto: {e}"))

# ── !gmplayers ────────────────────────────────────────────────────────────────

@bot.command(name='gmplayers')
async def cmd_gmplayers(ctx, *, name: str = ''):
    if not _in_channel(ctx):
        return
    if not name:
        uid      = str(ctx.author.id)
        profiles = _load_profiles()
        self_p   = profiles.get(uid, {})
        if not self_p.get('sheet_name'):
            await ctx.send(embed=_err("Usage: `!gmplayers <drafter name>` — or link yourself first with `!linkprofile <name>`"))
            return
        name = self_p['sheet_name']

    lottos = _load_lottos()
    counts = _get_drafter_players(name, lottos)

    if not counts:
        await ctx.send(embed=_err(f"❌ No lotto data found for **{name}**. Make sure lottos have been added with `!setlotto`."))
        return

    sorted_players = sorted(counts.items(), key=lambda x: (-x[1], _get_adp(x[0])))
    q = name.strip().lower()
    drafts_logged  = sum(
        1 for teams in lottos.values()
        for entry in teams.values()
        if any(q in d.lower() or d.lower() in q for d in entry.get('drafters', []))
    )

    all_lines = [
        f"{MEDALS[i] if i < 3 else f'{i+1}.'} {player:<24} ×{count}"
        for i, (player, count) in enumerate(sorted_players)
    ]
    subtitle = f"`{drafts_logged} drafts logged`"

    if len(all_lines) <= 10:
        block = '\n'.join(all_lines)
        embed = discord.Embed(
            title=f"🏀  {name} — Most Drafted Players",
            description=f"{subtitle}\n```\n{block}\n```",
            color=C_PROFILE
        )
        await ctx.send(embed=embed)
    else:
        view = PlayerListView(
            title=f"🏀  {name} — Most Drafted Players",
            lines=all_lines,
            subtitle=subtitle
        )
        await ctx.send(embed=view.get_embed(), view=view)

# ── !team ─────────────────────────────────────────────────────────────────────

@bot.command(name='team')
async def cmd_team(ctx, *, args: str = ''):
    if not _in_channel(ctx):
        return

    m = re.match(r'(?i)ATD\s*(\d+)\s+(.*)', args.strip())
    if not m:
        await ctx.send(embed=_err("Usage: `!team ATD <num> <team name>`\nExample: `!team ATD 15 Age is just a number`"))
        return

    draft_num  = m.group(1)
    team_query = m.group(2).strip()

    async with ctx.typing():
        loop = asyncio.get_running_loop()
        tab_title, tab_data = await loop.run_in_executor(None, _fetch_draft_tab, draft_num)

    if not tab_data:
        await ctx.send(embed=_err(f"❌ No roster tab found for **ATD {draft_num}** in the Google Sheet."))
        return

    actual_name, players = _find_team_roster(tab_data, team_query)

    if not actual_name:
        await ctx.send(embed=_err(f"❌ No team matching **{team_query}** found in ATD {draft_num}."))
        return

    def _emoji_display_local(tn):
        q2 = tn.strip().lower()
        for en, mapped in EMOJI_TEAM_MAP.items():
            if mapped.strip().lower() == q2:
                emoji = discord.utils.get(ctx.guild.emojis, name=en)
                if emoji:
                    return str(emoji)
        return ''

    emoji_str = _emoji_display_local(actual_name)
    roster    = '\n'.join(f"{i:>2}. {p['name']:<24} {p.get('year','')}" for i, p in enumerate(players, 1))

    embed = discord.Embed(
        title=f"{emoji_str}  {actual_name}",
        description=f"**ATD {draft_num}**" + (f"\n```\n{roster}\n```" if roster else ''),
        color=C_PROFILE,
    )
    embed.set_footer(text=f"Tab: {tab_title}")
    await ctx.send(embed=embed)

# ── !gmfind ───────────────────────────────────────────────────────────────────

@bot.command(name='gmfind')
async def cmd_gmfind(ctx, *, args: str = ''):
    if not _in_channel(ctx):
        return
    if not args:
        await ctx.send(embed=_err("Usage: `!gmfind <player>` — or `!gmfind <player> | <drafter>`"))
        return

    # Parse optional drafter: "Trae Young | Momo"
    if '|' in args:
        player_query, drafter_name = [s.strip() for s in args.split('|', 1)]
    else:
        player_query  = args.strip()
        drafter_name  = None
        uid           = str(ctx.author.id)
        profiles      = _load_profiles()
        self_p        = profiles.get(uid, {})
        if self_p.get('sheet_name'):
            drafter_name = self_p['sheet_name']

    pq     = player_query.lower()
    lottos = _load_lottos()

    def _emoji_display_local(team_name):
        q2 = team_name.strip().lower()
        for en, mapped in EMOJI_TEAM_MAP.items():
            if mapped.strip().lower() == q2:
                emoji = discord.utils.get(ctx.guild.emojis, name=en)
                if emoji:
                    return str(emoji)
        return ''

    found = []
    for dk, teams in sorted(lottos.items(), key=lambda x: int(re.search(r'\d+', x[0]).group())):
        for team_name, entry in teams.items():
            if drafter_name and not _drafter_match(drafter_name, entry.get('drafters', [])):
                continue
            for p in entry.get('players', []):
                if pq in p['name'].lower():
                    found.append((dk, team_name, p['name'], p.get('year', '')))
                    break

    if not found:
        scope = f"**{drafter_name}**'s teams" if drafter_name else "any stored lotto"
        await ctx.send(embed=_err(f"❌ No match for **{player_query}** in {scope}."))
        return

    display_player = found[0][2]
    lines = [
        f"`{dk:<8}` {_emoji_display_local(tn)}  {tn}  ({yr})"
        for dk, tn, _, yr in found
    ]

    scope_label = drafter_name or "all drafters"
    embed = discord.Embed(
        title=f"🔍  {display_player}",
        description=f"`{len(found)} team(s) · {scope_label}`\n\n" + '\n'.join(lines),
        color=C_PROFILE,
    )
    await ctx.send(embed=embed)

# ── !findplayer ───────────────────────────────────────────────────────────────

@bot.command(name='findplayer')
async def cmd_findplayer(ctx, *, player_name: str = ''):
    if not _in_channel(ctx):
        return
    if not player_name:
        await ctx.send(embed=_err("Usage: `!findplayer <player name>`"))
        return

    pq       = player_name.strip().lower()
    lottos   = _load_lottos()
    profiles = _load_profiles()

    matches = []  # (draft_key, drafters, player_display_name)
    for dk, teams in sorted(lottos.items(), key=lambda x: int(re.search(r'\d+', x[0]).group())):
        for team_name, entry in teams.items():
            for p in entry.get('players', []):
                if pq in p['name'].lower():
                    matches.append((dk, entry.get('drafters', []), p['name']))
                    break

    if not matches:
        await ctx.send(embed=_err(f"❌ No drafted history found for **{player_name}**."))
        return

    display_player = matches[0][2]

    drafter_counts = {}  # canonical name -> {'count': int, 'drafts': [dk, ...]}
    for dk, drafters_list, _ in matches:
        for raw_name in drafters_list:
            if raw_name.strip().lower() == 'deleted user':
                continue
            canon = _resolve_drafter_name(raw_name, profiles)
            info  = drafter_counts.setdefault(canon, {'count': 0, 'drafts': []})
            info['count'] += 1
            info['drafts'].append(dk)

    ranking = sorted(drafter_counts.items(), key=lambda x: x[1]['count'], reverse=True)[:10]

    lines = []
    for i, (name, info) in enumerate(ranking, 1):
        rank_str   = MEDALS[i - 1] if i <= 3 else f"`{i:>2}.`"
        drafts_str = ', '.join(info['drafts'])
        lines.append(f"{rank_str} **{name}** — {info['count']}x  ({drafts_str})")

    embed = discord.Embed(
        title=f"📊  {display_player} — Draft History",
        description=f"Drafted **{len(matches)}** time(s) total\n\n" + '\n'.join(lines),
        color=C_PROFILE,
    )
    await ctx.send(embed=embed)

# ── !gmteam ───────────────────────────────────────────────────────────────────

@bot.command(name='gmteam')
async def cmd_gmteam(ctx, *, args: str = ''):
    if not _in_channel(ctx):
        return

    # Default to self if no args
    if not args:
        uid      = str(ctx.author.id)
        profiles = _load_profiles()
        self_p   = profiles.get(uid, {})
        if not self_p.get('sheet_name'):
            await ctx.send(embed=_err("Usage: `!gmteam <name>` or `!gmteam ATD <num> <name>`\nOr link yourself with `!linkprofile <name>`."))
            return
        args = self_p['sheet_name']

    lottos = _load_lottos()

    # Parse: "ATD <num> <name>" or "<name> ATD <num>" or just "<name>"
    m = re.match(r'(?i)ATD\s+(\d+)\s+(.*)', args.strip())
    if m:
        draft_num, name = m.group(1), m.group(2).strip()
    else:
        m2 = re.match(r'(.*?)\s+ATD\s+(\d+)\s*$', args.strip(), re.IGNORECASE)
        if m2:
            name, draft_num = m2.group(1).strip(), m2.group(2)
        else:
            name, draft_num = args.strip(), None

    q = name.strip().lower()

    def _emoji_display(team_name):
        q = team_name.strip().lower()
        for en, mapped in EMOJI_TEAM_MAP.items():
            if mapped.strip().lower() == q:
                emoji = discord.utils.get(ctx.guild.emojis, name=en)
                if emoji:
                    return str(emoji)
        return ''

    if draft_num:
        draft_key  = f"ATD {draft_num}"
        draft_data = lottos.get(draft_key, {})
        if not draft_data:
            await ctx.send(embed=_err(f"❌ No lotto data for **{draft_key}**. Use `!setlotto` to add it."))
            return

        found_team, found_entry = None, None
        for team_name, entry in draft_data.items():
            if _drafter_match(q, entry.get('drafters', [])):
                found_team, found_entry = team_name, entry
                break

        if not found_team:
            await ctx.send(embed=_err(f"❌ **{name}** has no team in **{draft_key}**."))
            return

        emoji_str  = _emoji_display(found_team)
        co_owners  = [d for d in found_entry.get('drafters', [])
                      if q not in d.lower() and d.lower() not in q and d.strip().lower() != 'deleted user']
        co_str     = f"\nCo-owners: {', '.join(co_owners)}" if co_owners else ''
        players    = found_entry.get('players', [])
        roster     = '\n'.join(f"{i:>2}. {p['name']:<22} {p['year']}" for i, p in enumerate(players, 1))

        embed = discord.Embed(
            title=f"{emoji_str}  {found_team}",
            description=f"**{draft_key}  ·  {name}**{co_str}" + (f"\n```\n{roster}\n```" if roster else ''),
            color=C_PROFILE
        )
        await ctx.send(embed=embed)

    else:
        found = []
        for dk, draft_data in sorted(lottos.items(), key=lambda x: int(re.search(r'\d+', x[0]).group())):
            for team_name, entry in draft_data.items():
                if _drafter_match(q, entry.get('drafters', [])):
                    found.append((dk, team_name))

        if not found:
            await ctx.send(embed=_err(f"❌ No teams found for **{name}** in any stored lotto."))
            return

        lines = []
        for dk, team_name in found:
            emoji_str = _emoji_display(team_name)
            lines.append(f"`{dk:<8}` {emoji_str}  {team_name}")

        embed = discord.Embed(
            title=f"🏀  {name} — Drafted Teams",
            description=f"`{len(found)} team(s) across stored lottos`\n\n" + '\n'.join(lines),
            color=C_PROFILE
        )
        await ctx.send(embed=embed)

# ── !profile ──────────────────────────────────────────────────────────────────

@bot.command(name='profile')
async def cmd_profile(ctx, *, name: str = ''):
    if not _in_channel(ctx):
        return
    if not name:
        uid      = str(ctx.author.id)
        profiles = _load_profiles()
        self_p   = profiles.get(uid, {})
        if not self_p.get('sheet_name'):
            await ctx.send(embed=_err("Usage: `!profile <drafter name>` — or link yourself first with `!linkprofile <name>`"))
            return
        name = self_p['sheet_name']

    async with ctx.typing():
        raw = await _get_raw()
    drafters, _, _ = _parse(raw)

    d = _find(drafters, name)
    if not d:
        await ctx.send(embed=_err(f"❌ No drafter found matching **{name}**."))
        return

    profiles = _load_profiles()
    _, profile = _profile_by_name(profiles, d['name'])

    dw       = d['draft_wins']
    best_num = max(dw, key=dw.get) if dw else None
    wrst_num = min(dw, key=dw.get) if dw else None

    raw_emoji = profile.get('fav_emoji', '') if profile else ''
    if isinstance(raw_emoji, list):
        fav_emoji = ' '.join(raw_emoji)
    else:
        fav_emoji = raw_emoji
    title_name = f"{fav_emoji}  {d['name']}" if fav_emoji else f"🏀  {d['name']}"

    embed = discord.Embed(
        title=title_name,
        description=f"`{d['played']} drafts  ·  {d['total']}W  ·  {_pct_str(d['pct'])} win rate`",
        color=C_PROFILE
    )

    embed.add_field(name="🏆  Total Wins",  value=f"**{d['total']}W**",           inline=True)
    embed.add_field(name="📊  Win %",        value=f"**{_pct_str(d['pct'])}**",    inline=True)
    embed.add_field(name="⚡  Over .500",    value=f"**{_over_str(d['over'])}**",  inline=True)

    embed.add_field(name="📋  Drafts",       value=f"**{d['played']}**",           inline=True)
    if best_num:
        embed.add_field(name="🥇  Best",     value=f"**ATD {best_num}** — {dw[best_num]}W", inline=True)
    if wrst_num and wrst_num != best_num:
        embed.add_field(name="📉  Worst",    value=f"**ATD {wrst_num}** — {dw[wrst_num]}W", inline=True)

    embed.add_field(name="​", value="━" * 38, inline=False)

    if profile and profile.get('fav_players'):
        fav_lines = '\n'.join(f"• {p}" for p in profile['fav_players'])
        embed.add_field(name="⭐  Favourite Players", value=fav_lines, inline=False)
    else:
        embed.add_field(name="⭐  Favourite Players", value="*Not set — use `!favplayer`*", inline=False)

    # Most drafted players from lotto data
    lottos       = _load_lottos()
    player_counts = _get_drafter_players(d['name'], lottos)
    if player_counts:
        top5 = sorted(player_counts.items(), key=lambda x: (-x[1], _get_adp(x[0])))[:5]
        top5_lines = '\n'.join(
            f"{MEDALS[i] if i < 3 else f'{i+1}.'} {player:<22} ×{count}"
            for i, (player, count) in enumerate(top5)
        )
        embed.add_field(name="📌  Most Drafted Players", value=f"```\n{top5_lines}\n```", inline=False)

    wins, runner_ups = _get_champ_record(d['name'])
    if wins or runner_ups:
        def _fmt_drafts(nums):
            return '  '.join(f"D{n}" for n in nums) if nums else '—'
        champ_val = ''
        if wins:
            champ_val += f"🥇 **{len(wins)}×** — {_fmt_drafts(wins)}\n"
        if runner_ups:
            champ_val += f"🥈 **{len(runner_ups)}×** — {_fmt_drafts(runner_ups)}"
        embed.add_field(name="🏆  Championship Record", value=champ_val.strip(), inline=False)

    if profile and profile.get('sig_team'):
        st = profile['sig_team']
        roster_lines = '\n'.join(
            f"{i:>2}. {p['name']:<22} {p['year']}"
            for i, p in enumerate(st['players'], 1)
        )
        embed.add_field(
            name="🏆  Signature Team",
            value=f"**{st['draft']}  ·  {st['team_name']}**\n```\n{roster_lines}\n```",
            inline=False
        )
    else:
        embed.add_field(name="🏆  Signature Team", value="*Not set — use `!sigteam ATD <num> <emoji>`*", inline=False)

    embed.set_footer(text="ATD Win Bot  ·  !favplayer  ·  !sigteam  ·  !linkprofile")
    await ctx.send(embed=embed)

# ── !rings ────────────────────────────────────────────────────────────────────

@bot.command(name='rings')
async def cmd_rings(ctx):
    if not _in_channel(ctx):
        return

    lines = []
    for num in sorted(CHAMPIONS.keys()):
        data     = CHAMPIONS[num]
        winners  = data.get('w', [])
        rus      = data.get('ru', [])
        w_str    = ' & '.join(winners) if winners else '—'
        ru_str   = f"  (🥈 {' & '.join(rus)})" if rus else ''
        lines.append(f"D{num:<3}  🏆 {w_str}{ru_str}")

    crowned = sum(1 for d in CHAMPIONS.values() if d.get('w'))
    view = PlayerListView(
        title="🏆 ATD Champions — All Time",
        lines=lines,
        subtitle=f"`{crowned} drafts with a crowned champion`",
        per_page=15,
    )
    await ctx.send(embed=view.get_embed(), view=view)

# ── !winner / !loser ────────────────────────────────────────────────────────

_NON_PLAYER_RE = re.compile(r'^\s*group\s+\S+\s*$', re.IGNORECASE)


def _fuzzy_matches_any(name: str, candidates: list, threshold: float = 0.88) -> bool:
    """True if `name` closely matches any name in `candidates` (typo/spelling
    variant of the same person), not just an exact string match."""
    name_l = name.lower()
    for c in candidates:
        if difflib.SequenceMatcher(None, name_l, c).ratio() >= threshold:
            return True
    return False


def _dedupe_names(names: list, threshold: float = 0.88) -> list:
    """Cluster near-duplicate spellings of the same name (typos, apostrophe/
    hyphen variants) together, keeping the most common exact spelling per
    cluster as the canonical one. O(n^2) but the input here is small (a few
    hundred names at most)."""
    counts = Counter(names)
    clusters = []
    for name in counts:
        best_cluster, best_ratio = None, 0.0
        for ci, cluster in enumerate(clusters):
            for rep in cluster:
                ratio = difflib.SequenceMatcher(None, name.lower(), rep.lower()).ratio()
                if ratio > best_ratio:
                    best_ratio, best_cluster = ratio, ci
        if best_cluster is not None and best_ratio >= threshold:
            clusters[best_cluster].append(name)
        else:
            clusters.append([name])
    return [max(cluster, key=lambda n: (counts[n], -len(n))) for cluster in clusters]


def _all_winning_teams():
    """Yield (draft_label, team_name, players) for every winning team on
    record, expanding multi-winner (team draft) entries into one per team."""
    for num in sorted(CHAMPION_TEAMS.keys(), key=lambda k: (int(str(k).split('-')[0]), str(k))):
        entry = CHAMPION_TEAMS[num]
        label = f"ATD {num}"
        if 'teams' in entry:
            for t in entry['teams']:
                yield label, t['team'], t['players']
        else:
            yield label, entry['team'], entry['players']


def _build_win_counts() -> dict:
    """canonical player name -> {'count': int, 'drafts': [label, ...]}
    across every winning team on record, with near-duplicate spellings of
    the same player merged together. Pure CPU work — call via executor."""
    raw_counts = {}
    for label, team_name, players in _all_winning_teams():
        for p in players:
            info = raw_counts.setdefault(p['name'], {'count': 0, 'drafts': []})
            info['count'] += 1
            info['drafts'].append(label)

    canonical_names = _dedupe_names(list(raw_counts.keys()))
    canon_lookup = {}
    for canon in canonical_names:
        for raw in raw_counts:
            if difflib.SequenceMatcher(None, raw.lower(), canon.lower()).ratio() >= 0.88 or raw == canon:
                canon_lookup.setdefault(canon, []).append(raw)

    merged = {}
    for canon, raws in canon_lookup.items():
        total = 0
        drafts = []
        for raw in set(raws):
            total += raw_counts[raw]['count']
            drafts.extend(raw_counts[raw]['drafts'])
        merged[canon] = {'count': total, 'drafts': drafts}
    return merged


@bot.command(name='winner')
async def cmd_winner(ctx, *, args: str = ''):
    if not _in_channel(ctx):
        return

    args = args.strip()

    loop = asyncio.get_event_loop()

    if not args:
        counts = await loop.run_in_executor(None, _build_win_counts)

        ranking = sorted(counts.items(), key=lambda x: x[1]['count'], reverse=True)
        lines = []
        for i, (name, info) in enumerate(ranking, 1):
            draft_nums = ', '.join(d.replace('ATD ', '') for d in info['drafts'])
            lines.append(f"{i:>2}. {name:<26} {info['count']:>2}x")
            lines.append(f"      {draft_nums}")

        view = PlayerListView(
            title="🏆 Most Championships — Players",
            lines=lines,
            subtitle=f"`{len(counts)} unique players have won at least once  ·  {len(CHAMPION_TEAMS)} drafts tracked`",
            per_page=20,
        )
        await ctx.send(embed=view.get_embed(), view=view)
        return

    m = re.fullmatch(r'(?i)(?:ATD\s*)?(\d+)\s*(-?\s*D(\d+))?', args)
    if not m:
        # Not "ATD <number>" — treat it as a player name lookup.
        counts = await loop.run_in_executor(None, _build_win_counts)

        q = args.lower()
        match_name = next((name for name in counts if name.lower() == q), None)
        if not match_name:
            candidates = [name for name in counts if q in name.lower()]
            if len(candidates) == 1:
                match_name = candidates[0]
            elif len(candidates) > 1:
                await ctx.send(embed=_err(
                    f"❌ Multiple players match **{args}**: {', '.join(candidates)}. Be more specific."
                ))
                return

        if match_name:
            info = counts[match_name]
            embed = discord.Embed(
                title=f"🏆 {match_name}",
                description=f"**{info['count']}x** champion\n\nWon in: {', '.join(info['drafts'])}",
                color=C_GOLD,
            )
            await ctx.send(embed=embed)
            return

        await ctx.send(
            f"**{args}** has never won a ring 💍\n\n"
            f"{ctx.author.mention} — will you take the challenge of winning a ring with "
            f"**{args}**, or will you keep drafting the same players over and over?"
        )
        return

    num = int(m.group(1))
    d_suffix = f"-D{m.group(3)}" if m.group(3) else None
    key = f"{num}{d_suffix}" if d_suffix else num
    entry = CHAMPION_TEAMS.get(key, CHAMPION_TEAMS.get(num))

    if not entry:
        await ctx.send(embed=_err(f"❌ No winner recorded for ATD {num}."))
        return

    if 'teams' in entry:
        embed = discord.Embed(title=f"🏆 ATD {num} — Co-Champions", color=C_GOLD)
        for t in entry['teams']:
            roster = '\n'.join(f"{i}. {p['name']} ({p['year']})" for i, p in enumerate(t['players'], 1))
            embed.add_field(name=t['team'], value=f"```\n{roster}\n```", inline=False)
    else:
        roster = '\n'.join(f"{i}. {p['name']} ({p['year']})" for i, p in enumerate(entry['players'], 1))
        embed = discord.Embed(
            title=f"🏆 ATD {num} — {entry['team']}",
            description=f"```\n{roster}\n```",
            color=C_GOLD,
        )
    await ctx.send(embed=embed)


@bot.command(name='loser')
async def cmd_loser(ctx):
    if not _in_channel(ctx):
        return

    async with ctx.typing():
        winners = set()
        for _, _, players in _all_winning_teams():
            for p in players:
                winners.add(p['name'].strip().lower())

        lottos = _load_lottos()
        all_players = {}  # lowercase name -> display name
        for teams in lottos.values():
            for entry in teams.values():
                for p in entry.get('players', []):
                    name = p['name'].strip()
                    if name and not _NON_PLAYER_RE.match(name):
                        all_players.setdefault(name.lower(), name)
        # Fill in any players only known via champion_teams.py (pre-Sheet drafts)
        for _, _, players in _all_winning_teams():
            for p in players:
                name = p['name'].strip()
                if name and not _NON_PLAYER_RE.match(name):
                    all_players.setdefault(name.lower(), name)

        winners_list = list(winners)

        def _compute_never_won():
            raw_never_won = [
                name for key, name in all_players.items()
                if key not in winners and not _fuzzy_matches_any(name, winners_list)
            ]
            return sorted(_dedupe_names(raw_never_won))

        loop = asyncio.get_event_loop()
        never_won = await loop.run_in_executor(None, _compute_never_won)

    if not never_won:
        await ctx.send("Every drafted player on record has been on a winning team at least once!")
        return

    view = PlayerListView(
        title="😢 Never Won a Championship",
        lines=never_won,
        subtitle=f"`{len(never_won)} player(s) drafted at least once, never on a winning team`",
        per_page=20,
    )
    await ctx.send(embed=view.get_embed(), view=view)

# ── !winhelp ──────────────────────────────────────────────────────────────────

@bot.command(name='refreshrosters')
async def cmd_refreshrosters(ctx):
    if not _in_channel(ctx):
        return
    async with ctx.typing():
        lottos = _load_lottos()
        updated = 0
        skipped = 0
        loop = asyncio.get_running_loop()
        # Collect which draft numbers we actually need
        needed = set()
        for dk in lottos:
            m = re.search(r'\d+', dk)
            if m:
                needed.add(m.group(0))
        all_tabs = await loop.run_in_executor(None, _fetch_all_roster_tabs, needed)
        for draft_key, teams in lottos.items():
            m = re.search(r'\d+', draft_key)
            if not m:
                continue
            draft_num = m.group(0)
            tab_data = all_tabs.get(draft_num)
            if not tab_data:
                skipped += 1
                continue
            for team_name, entry in teams.items():
                if not entry.get('players'):
                    _, players = _find_team_roster(tab_data, team_name)
                    if players:
                        entry['players'] = players
                        updated += 1
        _save_lottos(lottos)
    embed = discord.Embed(
        title="✅ Rosters Refreshed",
        description=f"**{updated}** teams updated · **{skipped}** drafts with no tab found.",
        color=C_GREEN
    )
    await ctx.send(embed=embed)

@bot.command(name='importlottos')
async def cmd_importlottos(ctx):
    if not _in_channel(ctx):
        return
    if not ctx.message.attachments:
        await ctx.send(embed=_err("Attach a `lottos.json` file to this message."))
        return
    att = ctx.message.attachments[0]
    if not att.filename.endswith('.json'):
        await ctx.send(embed=_err("Attachment must be a `.json` file."))
        return
    async with ctx.typing():
        data = await att.read()
        try:
            parsed = json.loads(data.decode('utf-8'))
        except Exception as e:
            await ctx.send(embed=_err(f"Invalid JSON: {e}"))
            return
        with open(LOTTOS_FILE, 'w', encoding='utf-8') as f:
            json.dump(parsed, f, indent=2, ensure_ascii=False)
    total_teams = sum(len(t) for t in parsed.values())
    embed = discord.Embed(
        title="✅ Lottos Imported",
        description=f"**{len(parsed)}** drafts · **{total_teams}** teams loaded.",
        color=C_GREEN
    )
    await ctx.send(embed=embed)

@bot.command(name='sheettabs')
async def cmd_sheettabs(ctx):
    """List all worksheet tabs found in the roster Google Sheet."""
    if not _in_channel(ctx):
        return
    async with ctx.typing():
        loop = asyncio.get_running_loop()
        def _list_tabs():
            creds  = ServiceAccountCredentials.from_json_keyfile_name(SERVICE_ACCOUNT_FILE, SCOPE)
            client = gspread.authorize(creds)
            sh     = client.open_by_key(ROSTER_SHEET_ID)
            return [ws.title for ws in sh.worksheets()]
        titles = await loop.run_in_executor(None, _list_tabs)
    atd_tabs = [t for t in titles if re.match(r'ATD\s*\d+', t, re.IGNORECASE)]
    other_tabs = [t for t in titles if not re.match(r'ATD\s*\d+', t, re.IGNORECASE)]
    desc = f"**{len(atd_tabs)} ATD tabs:**\n" + ', '.join(atd_tabs[:50])
    if other_tabs:
        desc += f"\n\n**{len(other_tabs)} other tabs:**\n" + ', '.join(other_tabs[:20])
    embed = discord.Embed(title="📋 Roster Sheet Tabs", description=desc, color=C_BLUE)
    await ctx.send(embed=embed)

# ── !adminset  (admin only — edit another user's profile) ────────────────────

@bot.command(name='adminset')
async def cmd_adminset(ctx, *, args: str = ''):
    if not _in_channel(ctx):
        return
    if not ctx.author.guild_permissions.administrator:
        await ctx.send(embed=_err('❌ `!adminset` is admin only.'))
        return

    FIELDS = ['sigteam', 'favemoji', 'favplayer', 'clearfavplayers', 'clearemoji']
    parts  = args.strip().split()

    field_idx = next((i for i, p in enumerate(parts) if p.lower() in FIELDS), None)
    if field_idx is None or field_idx == 0:
        await ctx.send(embed=_err(
            'Usage:\n'
            '`!adminset <name> sigteam ATD <num> <emoji>`\n'
            '`!adminset <name> favemoji <emoji>`\n'
            '`!adminset <name> favplayer <player>`\n'
            '`!adminset <name> clearfavplayers`\n'
            '`!adminset <name> clearemoji`'
        ))
        return

    target_name = ' '.join(parts[:field_idx])
    field       = parts[field_idx].lower()
    value       = ' '.join(parts[field_idx + 1:]).strip()

    profiles = _load_profiles()
    uid, profile = _profile_by_name(profiles, target_name)
    if not uid:
        await ctx.send(embed=_err(f"❌ No linked profile found for **{target_name}**."))
        return

    display = profile.get('sheet_name', target_name)

    # ── clearfavplayers ──────────────────────────────────────────────────────
    if field == 'clearfavplayers':
        profiles[uid]['fav_players'] = []
        _save_profiles(profiles)
        await ctx.send(embed=discord.Embed(
            title='✅ Cleared',
            description=f"Favourite players cleared for **{display}**.",
            color=C_GREEN,
        ))
        return

    # ── clearemoji ───────────────────────────────────────────────────────────
    if field == 'clearemoji':
        profiles[uid]['fav_emoji'] = []
        _save_profiles(profiles)
        await ctx.send(embed=discord.Embed(
            title='✅ Cleared',
            description=f"Profile emoji cleared for **{display}**.",
            color=C_GREEN,
        ))
        return

    if not value:
        await ctx.send(embed=_err(f"❌ No value provided for `{field}`."))
        return

    # ── favemoji ─────────────────────────────────────────────────────────────
    if field == 'favemoji':
        existing = profiles[uid].get('fav_emoji', [])
        if isinstance(existing, str):
            existing = [existing] if existing else []
        if value not in existing:
            existing.append(value)
        profiles[uid]['fav_emoji'] = existing
        _save_profiles(profiles)
        await ctx.send(embed=discord.Embed(
            title=f"{' '.join(existing)}  Emoji set",
            description=f"Profile emoji updated for **{display}**.",
            color=C_GREEN,
        ))
        return

    # ── favplayer ─────────────────────────────────────────────────────────────
    if field == 'favplayer':
        new_players = [p.strip() for p in value.split(',') if p.strip()]
        existing       = profiles[uid].get('fav_players', [])
        existing_lower = {p.lower() for p in existing}
        added, rejected = [], []
        for raw in new_players:
            canonical_key, display_name = _lookup_adp_player(raw)
            if canonical_key:
                if display_name.lower() not in existing_lower:
                    existing.append(display_name)
                    existing_lower.add(display_name.lower())
                    added.append(display_name)
            else:
                rejected.append(f"**{raw}** not recognised as a basketball player.")
        profiles[uid]['fav_players'] = existing
        _save_profiles(profiles)
        lines = []
        if added:
            lines.append("✅ Added: " + ', '.join(f"**{p}**" for p in added))
        lines.extend(rejected)
        await ctx.send(embed=discord.Embed(
            title=f"⭐ Favourite Players — {display}",
            description='\n'.join(lines) or "No changes.",
            color=C_GREEN,
        ))
        return

    # ── sigteam ──────────────────────────────────────────────────────────────
    if field == 'sigteam':
        sig_m = re.match(r'(?i)ATD\s*(\d+)\s+(.*)', value)
        if not sig_m:
            await ctx.send(embed=_err("Usage: `!adminset <name> sigteam ATD <num> <emoji>`"))
            return
        draft_num = sig_m.group(1)
        team_raw  = sig_m.group(2).strip()

        async with ctx.typing():
            loop = asyncio.get_running_loop()
            tab_title, tab_data = await loop.run_in_executor(None, _fetch_draft_tab, draft_num)

        if not tab_data:
            await ctx.send(embed=_err(f"❌ No roster tab found for **ATD {draft_num}**."))
            return

        team_name_resolved = _resolve_emoji(team_raw)
        actual_name, players = _find_team_roster(tab_data, team_name_resolved)

        if not actual_name:
            await ctx.send(embed=_err(f"❌ Team **{team_name_resolved}** not found in ATD {draft_num}."))
            return

        profiles[uid]['sig_team'] = {
            'draft':     f"ATD {draft_num}",
            'tab_title': tab_title,
            'team_name': actual_name,
            'players':   players,
        }
        _save_profiles(profiles)

        roster = '\n'.join(f"{i:>2}. {p['name']:<22} {p['year']}" for i, p in enumerate(players, 1))
        embed  = discord.Embed(
            title=f"🏆 Signature Team Set — {display}",
            description=f"**ATD {draft_num} — {actual_name}**\n```\n{roster}\n```",
            color=C_GREEN,
        )
        await ctx.send(embed=embed)

# ── !addteam  (admin only) ────────────────────────────────────────────────────

# ── !adminlink  (admin only — link a Discord ID to a Win Sheet name) ─────────

@bot.command(name='adminlink')
async def cmd_adminlink(ctx, *, args: str = ''):
    if not _in_channel(ctx):
        return
    if not ctx.author.guild_permissions.administrator:
        await ctx.send(embed=_err('❌ `!adminlink` is admin only.'))
        return

    parts = args.strip().split(None, 1)
    if len(parts) < 2:
        await ctx.send(embed=_err('Usage: `!adminlink <Discord User ID> <Win Sheet name>`\nExample: `!adminlink 123456789012345678 Easy`'))
        return

    target_id, sheet_name = parts[0].strip(), parts[1].strip()
    if not target_id.isdigit():
        await ctx.send(embed=_err('❌ First argument must be the Discord User ID (numbers only).\nRight-click the user → Copy User ID (needs Developer Mode on).'))
        return

    async with ctx.typing():
        raw = await _get_raw()
    drafters, _, _ = _parse(raw)

    d = _find(drafters, sheet_name)
    if not d:
        await ctx.send(embed=_err(f"❌ No drafter found matching **{sheet_name}** on the Win Sheet.\nCheck the spelling and try again."))
        return

    profiles = _load_profiles()
    existing = profiles.get(target_id, {})
    profiles[target_id] = {**existing, 'sheet_name': d['name']}
    _save_profiles(profiles)

    embed = discord.Embed(
        title="✅ Profile Linked",
        description=f"Discord ID `{target_id}` is now linked to **{d['name']}** on the Win Sheet.",
        color=C_PROFILE,
    )
    await ctx.send(embed=embed)

@bot.command(name='addteam')
async def cmd_addteam(ctx, *, args: str = ''):
    if not _in_channel(ctx):
        return
    if not ctx.author.guild_permissions.administrator:
        await ctx.send(embed=_err('❌ `!addteam` is admin only.'))
        return

    # Format: ATD <num> <team name> | <GM1>, <GM2>
    m = re.match(r'(?i)(ATD\s*\d+)\s+(.+?)\s*\|\s*(.+)', args.strip())
    if not m:
        await ctx.send(embed=_err('Usage: `!addteam ATD <num> <team name> | <GM1>, <GM2>`\nExample: `!addteam ATD 85 Chicago Sky | Morgan`'))
        return

    draft_key = re.sub(r'\s+', ' ', m.group(1).strip().upper().replace('ATD', 'ATD'))
    # Normalise to "ATD <num>"
    draft_key = re.sub(r'ATD(\d+)', r'ATD \1', draft_key).strip()
    team_name = m.group(2).strip()
    gms       = [g.strip() for g in m.group(3).split(',') if g.strip()]

    lottos = _load_lottos()
    if draft_key not in lottos:
        lottos[draft_key] = {}

    existing = lottos[draft_key].get(team_name, {})
    existing['drafters'] = gms
    if 'players' not in existing:
        existing['players'] = []
    lottos[draft_key][team_name] = existing
    _save_lottos(lottos)

    embed = discord.Embed(
        title='✅ Team Added',
        description=f"**{team_name}** → {', '.join(gms)}\nDraft: **{draft_key}**",
        color=C_GREEN,
    )
    await ctx.send(embed=embed)

# ── !edit  (admin only — interactive GM editor) ───────────────────────────────

class _GMEditModal(discord.ui.Modal, title='Edit GM Assignment'):
    def __init__(self, idx: int, display_label: str, current_gms: str, view: 'discord.ui.View'):
        super().__init__()
        self._idx  = idx
        self._view = view
        self.gm_input = discord.ui.TextInput(
            label=display_label[:45],
            placeholder='GM names separated by commas',
            default=current_gms,
            required=False,
            max_length=500,
        )
        self.add_item(self.gm_input)

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.gm_input.value.strip()
        self._view.pending[self._idx] = [n.strip() for n in raw.split(',') if n.strip()] if raw else []
        self._view._rebuild()
        await interaction.response.defer()
        await self._view.message.edit(embed=self._view._build_embed(), view=self._view)


class _DraftEditView(discord.ui.View):
    def __init__(self, draft_key: str, teams: list, author_id: int):
        """
        teams: [(team_name, entry, source_key), ...]
        source_key is the actual lottos[] entry this team's data lives under —
        usually == draft_key, but may be a split-draft sibling (e.g. "ATD 61-D2")
        when this view was opened as a merged multi-draft editor.
        """
        super().__init__(timeout=1800)
        self.draft_key = draft_key
        self.teams     = teams
        self.author_id = author_id
        self.page      = 0
        self.pending   = {}          # index (int) -> new drafter list
        self.message   = None
        self.multi     = len({sk for _, _, sk in teams}) > 1
        self._rebuild()

    def _drafters(self, idx, entry):
        return self.pending.get(idx, entry.get('drafters', []))

    def _label(self, team_name, source_key):
        if not self.multi:
            return team_name
        m = re.search(r'-D(\d+)$', source_key, re.IGNORECASE)
        return f"{team_name} [D{m.group(1)}]" if m else f"{team_name} [D1]"

    def _build_embed(self):
        lines = []
        for idx, (team_name, entry, source_key) in enumerate(self.teams):
            gm_str = ', '.join(self._drafters(idx, entry)) or '—'
            mark   = '✏️ ' if idx in self.pending else ''
            lines.append(f"{mark}**{self._label(team_name, source_key)}** → {gm_str}")
        n_pages = max(1, (len(self.teams) - 1) // 25 + 1)
        embed = discord.Embed(
            title=f'✏️  Edit {self.draft_key}',
            description='\n'.join(lines),
            color=C_BLUE,
        )
        embed.set_footer(text=f'Page {self.page + 1}/{n_pages}  ·  {len(self.pending)} change(s) pending  ·  Select a team below to edit')
        return embed

    def _rebuild(self):
        self.clear_items()
        start      = self.page * 25
        page_teams = list(enumerate(self.teams))[start:start + 25]
        options = []
        for idx, (team_name, entry, source_key) in page_teams:
            gm_str  = ', '.join(self._drafters(idx, entry))[:99] or 'No GM'
            changed = '✏️ ' if idx in self.pending else ''
            options.append(discord.SelectOption(
                label=(changed + self._label(team_name, source_key))[:100],
                value=str(idx),
                description=gm_str,
            ))
        sel = discord.ui.Select(placeholder='Select a team to edit…', options=options, row=0)
        sel.callback = self._on_select
        self.add_item(sel)

        if self.page > 0:
            btn = discord.ui.Button(label='◀ Prev', style=discord.ButtonStyle.secondary, row=1)
            btn.callback = self._on_prev
            self.add_item(btn)
        if start + 25 < len(self.teams):
            btn = discord.ui.Button(label='Next ▶', style=discord.ButtonStyle.secondary, row=1)
            btn.callback = self._on_next
            self.add_item(btn)

        save = discord.ui.Button(label='💾  Save', style=discord.ButtonStyle.green, row=2)
        save.callback = self._on_save
        self.add_item(save)
        cxl = discord.ui.Button(label='✖  Cancel', style=discord.ButtonStyle.red, row=2)
        cxl.callback = self._on_cancel
        self.add_item(cxl)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message('❌ Only the admin who opened this editor can use it.', ephemeral=True)
            return False
        return True

    async def _on_select(self, interaction: discord.Interaction):
        idx = int(interaction.data['values'][0])
        team_name, entry, source_key = self.teams[idx]
        current = ', '.join(self._drafters(idx, entry))
        await interaction.response.send_modal(
            _GMEditModal(idx, self._label(team_name, source_key), current, self)
        )

    async def _on_prev(self, interaction: discord.Interaction):
        self.page -= 1
        self._rebuild()
        await interaction.response.edit_message(embed=self._build_embed(), view=self)

    async def _on_next(self, interaction: discord.Interaction):
        self.page += 1
        self._rebuild()
        await interaction.response.edit_message(embed=self._build_embed(), view=self)

    async def _on_save(self, interaction: discord.Interaction):
        if not self.pending:
            await interaction.response.send_message('No changes to save.', ephemeral=True)
            return
        lottos = _load_lottos()
        touched_keys = set()
        for idx, new_drafters in self.pending.items():
            team_name, _, source_key = self.teams[idx]
            draft = lottos.get(source_key, {})
            if team_name in draft:
                draft[team_name]['drafters'] = new_drafters
                touched_keys.add(source_key)
        _save_lottos(lottos)
        keys_str = ', '.join(sorted(touched_keys)) if touched_keys else self.draft_key
        embed = discord.Embed(
            title='✅ Saved',
            description=f'**{len(self.pending)}** change(s) saved to **{keys_str}**.',
            color=C_GREEN,
        )
        await interaction.response.edit_message(embed=embed, view=None)
        self.stop()

    async def _on_cancel(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title='✖  Cancelled',
            description='No changes were saved.',
            color=discord.Color.red(),
        )
        await interaction.response.edit_message(embed=embed, view=None)
        self.stop()

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass


@bot.command(name='edit')
async def cmd_edit(ctx, *, draft: str = ''):
    if not _in_channel(ctx):
        return
    if not ctx.author.guild_permissions.administrator:
        await ctx.send(embed=_err('❌ `!edit` is admin only.'))
        return
    if not draft:
        await ctx.send(embed=_err('Usage: `!edit ATD 104`'))
        return
    lottos = _load_lottos()
    key = next((k for k in lottos if k.lower() == draft.strip().lower()), None)
    if not key:
        await ctx.send(embed=_err(f'No lotto data found for **{draft.strip()}**. Use exactly as stored, e.g. `ATD 104` or `ATD 67-D2`.'))
        return

    # A base draft key (no -D2/-D3 suffix) auto-merges any split-draft siblings
    # into one combined editor (e.g. `!edit ATD 61` combines "ATD 61" + "ATD 61-D2").
    # Edits still save back to whichever original entry each team came from.
    # Editing a specific half directly (`!edit ATD 61-D2`) stays single.
    sibling_keys = []
    if not re.search(r'-D\d+$', key, re.IGNORECASE):
        prefix = (key + '-D').lower()
        sibling_keys = sorted(
            (k for k in lottos if k.lower().startswith(prefix)),
            key=lambda k: k.lower(),
        )

    teams = []
    for k in [key] + sibling_keys:
        for team_name, entry in sorted(lottos[k].items(), key=lambda x: x[0]):
            teams.append((team_name, entry, k))

    view = _DraftEditView(key, teams, ctx.author.id)
    msg  = await ctx.send(embed=view._build_embed(), view=view)
    view.message = msg

@bot.command(name='winhelp')
async def cmd_winhelp(ctx):
    if not _in_channel(ctx):
        return
    embed = discord.Embed(title="📖 ATD Win Bot — Commands", color=C_BLUE)
    embed.add_field(name="📊 Stats", inline=False, value=(
        "`!standings` — Leaderboard by total wins\n"
        "`!standings pct` — Leaderboard by win %\n"
        "`!standings recent` — Last 5 drafts\n"
        "`!standings recent <num>` — Last <num> drafts, e.g. `!standings recent 10`\n"
        "`!winrate` — Win rate leaderboard (min 350 wins)\n"
        "`!record <name>` — Full all-time record\n"
        "`!ranks <name>` — Where a drafter ranks\n"
        "`!season <num>` — Results for a specific draft\n"
        "`!findwin ATD <num>` — Your wins in a specific draft\n"
        "`!findwin ATD <num> <name>` — Someone else's wins in a specific draft\n"
        "`!compare <n1> vs <n2>` — Head-to-head\n"
        "`!winstats` — League-wide highlights"
    ))
    embed.add_field(name="🏅 Rankings", inline=False, value=(
        "`!above500` — Drafters with a winning record\n"
        "`!below500` — Drafters with a losing record\n"
        "`!historys <name>` — Full draft history\n"
        "`!rings` — Every ATD champion all time\n"
        "`!winner` — Players who've been on a winning team the most\n"
        "`!winner ATD <num>` — The winning team and roster for a specific draft\n"
        "`!winner <player>` — How many ATDs a player has won, and which ones\n"
        "`!loser` — Players never on a winning team\n"
        "`!drafts` — List all draft numbers\n"
        "`!active` — Most active drafters"
    ))
    embed.add_field(name="👤 Profile", inline=False, value=(
        "`!linkprofile <name>` — Link your Discord to your Win Sheet name\n"
        "`!profile` — Your profile card\n"
        "`!profile <name>` — Anyone's profile card\n"
        "`!favplayer <player>` — Add a favourite player (stacks)\n"
        "`!clearfavplayers` — Reset favourite players\n"
        "`!favemoji <emoji>` — Add an emoji to your profile (stacks)\n"
        "`!clearemoji` — Reset profile emoji\n"
        "`!sigteam ATD <num> <emoji>` — Set your signature team\n"
        "`!gmplayers <name>` — Most drafted players across all lottos\n"
        "`!gmfind <player>` — Which of your teams had a specific player\n"
        "`!gmfind <player> | <drafter>` — Same but for someone else\n"
        "`!findplayer <player>` — Who has drafted a player the most, league-wide\n"
        "`!team ATD <num> <team name>` — Look up a team by name in a draft\n"
        "`!gmteam <name>` — All teams a GM has drafted\n"
        "`!gmteam ATD <num> <name>` — Specific team a GM drafted in one draft"
    ))
    embed.add_field(name="⚙️ Admin", inline=False, value=(
        "`!setlotto ATD <num>` — Import a draft lotto (paste lines below)\n"
        "`!importlottos` — Upload a lottos.json file (attach to message)\n"
        "`!refreshrosters` — Pull latest rosters from the Google Sheet\n"
        "`!edit ATD <num>` — Interactive GM editor for a draft\n"
        "`!addteam ATD <num> <team> | <GM>` — Add a team to a draft manually\n"
        "`!adminset <name> sigteam ATD <num> <emoji>` — Set someone's sig team\n"
        "`!adminset <name> favemoji <emoji>` — Set someone's profile emoji\n"
        "`!adminset <name> favplayer <player>` — Add to someone's fav players\n"
        "`!adminset <name> clearfavplayers` — Clear someone's fav players\n"
        "`!adminset <name> clearemoji` — Clear someone's profile emoji\n"
        "`!adminlink <Discord ID> <name>` — Link a Discord ID to a Win Sheet name (for offline users)"
    ))
    await ctx.send(embed=embed)

# ── Error handler ─────────────────────────────────────────────────────────────

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(embed=_err("⚠️ Missing argument. Try `!winhelp` for usage."))
        return
    if isinstance(error, commands.CommandInvokeError):
        print(f"[error] {ctx.command}: {error.original}")
        await ctx.send(embed=_err("⚠️ Something went wrong. Please try again."))
        return
    raise error

bot.run(DISCORD_TOKEN)
