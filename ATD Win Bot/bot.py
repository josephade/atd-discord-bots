import discord
from discord.ext import commands
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import asyncio
from config import DISCORD_TOKEN, DISCORD_GUILD_ID, SPREADSHEET_ID, SERVICE_ACCOUNT_FILE

SCOPE         = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
WIN_SHEET_TAB = 'Win Sheet'
MIN_PCT_GAMES = 300
RECENT_COUNT  = 5

# Embed colours
C_GOLD   = 0xFFD700
C_TEAL   = 0x1ABC9C
C_BLUE   = 0x3498DB
C_ORANGE = 0xFF8C00
C_PURPLE = 0x9B59B6
C_RED    = 0xE74C3C
C_GREEN  = 0x2ECC71
C_GRAY   = 0x95A5A6

MEDALS = ['🥇', '🥈', '🥉']

# ── Sheet fetching ────────────────────────────────────────────────────────────

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

    headers = raw[0]
    draft_col = {}
    for i, h in enumerate(headers):
        h = h.strip()
        if h.startswith('Draft '):
            try:
                draft_col[int(h.replace('Draft ', '').strip())] = i
            except ValueError:
                pass

    draft_numbers = sorted(draft_col)
    recent_drafts = draft_numbers[-RECENT_COUNT:]

    # Locate summary columns dynamically so adding new draft columns doesn't break them
    h_lower = [h.strip().lower() for h in headers]
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
    q = query.strip().lower()
    for d in drafters:
        if d['name'].lower() == q:
            return d
    for d in drafters:
        if q in d['name'].lower():
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

# ── !standings ─────────────────────────────────────────────────────────────────

@bot.command(name='standings')
async def cmd_standings(ctx, mode: str = ''):
    if not _in_channel(ctx):
        return

    async with ctx.typing():
        raw = await _get_raw()
    drafters, _, recent_drafts = _parse(raw)

    if not drafters:
        await ctx.send(embed=_err("⚠️ Could not load standings data."))
        return

    mode = mode.lower()

    if mode == 'pct':
        pool = sorted(
            [d for d in drafters if d['total'] >= MIN_PCT_GAMES and d['pct'] is not None],
            key=lambda d: d['pct'], reverse=True
        )
        title    = "📊 ATD Standings — Win %"
        footer   = f"Min {MIN_PCT_GAMES} wins to qualify · {len(pool)} qualified drafters"
        color    = C_TEAL
        medal_fn = lambda d: f"{d['total']}W · {_pct_str(d['pct'])} · {_over_str(d['over'])}"
        row_fn   = lambda i, d: f"{i:>2}. {d['name']:<22}  {d['total']:>4}W  {_pct_str(d['pct']):>7}  {_over_str(d['over']):>6}"

    elif mode == 'recent':
        rd    = f"Drafts {recent_drafts[0]}–{recent_drafts[-1]}"
        pool  = sorted(
            [d for d in drafters if d['recent_wins'] > 0],
            key=lambda d: d['recent_wins'], reverse=True
        )
        title    = f"📊 ATD Standings — Recent ({rd})"
        footer   = f"{len(pool)} active drafters in last {RECENT_COUNT} drafts"
        color    = C_BLUE
        medal_fn = lambda d: f"{d['recent_wins']}W"
        row_fn   = lambda i, d: f"{i:>2}. {d['name']:<22}  {d['recent_wins']:>3}W"

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

# ── !record ────────────────────────────────────────────────────────────────────

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

    embed.add_field(name="Total Wins",          value=f"**{d['total']}W**",                    inline=True)
    embed.add_field(name="Win %",               value=f"**{_pct_str(d['pct'])}**",             inline=True)
    embed.add_field(name="Wins over .500",      value=f"**{_over_str(d['over'])}**",           inline=True)

    embed.add_field(name="Drafts Played",       value=str(d['played']),                         inline=True)
    embed.add_field(name=f"Recent ({rd_label})",value=f"{d['recent_wins']}W",                   inline=True)

    if best_num is not None:
        embed.add_field(name="Best Draft",      value=f"Draft {best_num} — {dw[best_num]}W",   inline=True)
    if wrst_num is not None and wrst_num != best_num:
        embed.add_field(name="Worst Draft",     value=f"Draft {wrst_num} — {dw[wrst_num]}W",   inline=True)

    await ctx.send(embed=embed)

# ── !season ────────────────────────────────────────────────────────────────────

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

# ── !compare ───────────────────────────────────────────────────────────────────

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
        ("Total Wins",   f"{d1['total']}W",       f"{d2['total']}W"),
        ("Win %",        _pct_str(d1['pct']),      _pct_str(d2['pct'])),
        ("Over .500",    _over_str(d1['over']),    _over_str(d2['over'])),
        ("Drafts",       str(d1['played']),         str(d2['played'])),
        ("Best Draft",   best(d1),                  best(d2)),
        ("Worst Draft",  worst(d1),                 worst(d2)),
    ]

    lw = max(len(r[0]) for r in rows)
    vw = max(max(len(r[1]), len(r[2])) for r in rows)
    n1 = d1['name'][:vw]
    n2 = d2['name'][:vw]

    header = f"{'':>{lw}}  {n1:>{vw}}  {n2:>{vw}}"
    sep    = '─' * len(header)
    lines  = [f"{r[0]:<{lw}}  {r[1]:>{vw}}  {r[2]:>{vw}}" for r in rows]
    body   = '\n'.join(lines)

    embed = discord.Embed(
        title=f"⚔️ {d1['name']} vs {d2['name']}",
        description=f"```\n{header}\n{sep}\n{body}\n```",
        color=C_RED
    )
    await ctx.send(embed=embed)

# ── !winstats ──────────────────────────────────────────────────────────────────

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
    by_recent = sorted([d for d in drafters if d['recent_wins'] > 0],
                       key=lambda d: d['recent_wins'], reverse=True)
    rd = f"D{recent_drafts[0]}–{recent_drafts[-1]}"

    embed = discord.Embed(title="📈 ATD League Win Stats", color=C_GREEN)
    embed.add_field(name="👑 Most Wins All-Time",
                    value=f"**{by_total[0]['name']}**\n{by_total[0]['total']}W", inline=True)
    if by_pct:
        embed.add_field(name=f"📈 Best Win % (≥{MIN_PCT_GAMES}W)",
                        value=f"**{by_pct[0]['name']}**\n{_pct_str(by_pct[0]['pct'])}", inline=True)
    if by_recent:
        embed.add_field(name=f"🔥 Recent Hot Streak ({rd})",
                        value=f"**{by_recent[0]['name']}**\n{by_recent[0]['recent_wins']}W", inline=True)
    embed.add_field(name="📉 Fewest Wins All-Time",
                    value=f"**{by_total[-1]['name']}**\n{by_total[-1]['total']}W", inline=True)
    embed.add_field(name="👥 Total Drafters",
                    value=str(len(drafters)), inline=True)
    await ctx.send(embed=embed)

# ── !rank ─────────────────────────────────────────────────────────────────────

@bot.command(name='ranks')
async def cmd_rank(ctx, *, name: str = ''):
    if not _in_channel(ctx):
        return
    if not name:
        await ctx.send(embed=_err("Usage: `!ranks <drafter name>`"))
        return

    async with ctx.typing():
        raw = await _get_raw()
    drafters, _, _ = _parse(raw)

    d = _find(drafters, name)
    if not d:
        await ctx.send(embed=_err(f"❌ No drafter found matching **{name}**."))
        return

    total = len(drafters)
    by_wins = sorted(drafters, key=lambda x: x['total'], reverse=True)
    by_pct  = sorted([x for x in drafters if x['pct'] is not None], key=lambda x: x['pct'], reverse=True)
    by_over = sorted([x for x in drafters if x['over'] is not None], key=lambda x: x['over'], reverse=True)

    rank_wins = next((i + 1 for i, x in enumerate(by_wins) if x['name'] == d['name']), None)
    rank_pct  = next((i + 1 for i, x in enumerate(by_pct)  if x['name'] == d['name']), None)
    rank_over = next((i + 1 for i, x in enumerate(by_over) if x['name'] == d['name']), None)

    def rank_label(r, n):
        if r is None:
            return 'N/A'
        pct = round((1 - (r - 1) / n) * 100)
        return f"**#{r}** of {n}  *(top {pct}%)*"

    embed = discord.Embed(title=f"🏅 {d['name']} — Rankings", color=0x5865F2)
    embed.add_field(name="Total Wins",     value=f"{rank_label(rank_wins, total)}\n{d['total']}W",      inline=False)
    embed.add_field(name="Win %",          value=f"{rank_label(rank_pct, len(by_pct))}\n{_pct_str(d['pct'])}", inline=False)
    embed.add_field(name="Wins over .500", value=f"{rank_label(rank_over, len(by_over))}\n{_over_str(d['over'])}", inline=False)
    await ctx.send(embed=embed)

# ── !history ──────────────────────────────────────────────────────────────────

@bot.command(name='historys')
async def cmd_history(ctx, *, name: str = ''):
    if not _in_channel(ctx):
        return
    if not name:
        await ctx.send(embed=_err("Usage: `!historys <drafter name>`"))
        return

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

    played = sorted(dw.keys())
    best_num  = max(dw, key=dw.get)
    worst_num = min(dw, key=dw.get)

    # Build rows of 4 drafts per line
    entries = [f"D{n}: {dw[n]}W{'★' if n == best_num else ('▼' if n == worst_num else '')}" for n in played]
    per_row = 4
    rows = [entries[i:i + per_row] for i in range(0, len(entries), per_row)]
    table = '\n'.join('  '.join(f"{e:<10}" for e in row) for row in rows)

    embed = discord.Embed(
        title=f"📅 {d['name']} — Career History",
        description=f"```\n{table}\n```",
        color=0xF39C12
    )
    embed.add_field(name="Drafts Played", value=str(len(played)),                        inline=True)
    embed.add_field(name="Best",          value=f"Draft {best_num} ({dw[best_num]}W ★)", inline=True)
    embed.add_field(name="Worst",         value=f"Draft {worst_num} ({dw[worst_num]}W ▼)", inline=True)
    await ctx.send(embed=embed)

# ── !above500 ─────────────────────────────────────────────────────────────────

@bot.command(name='above500')
async def cmd_above500(ctx):
    if not _in_channel(ctx):
        return

    async with ctx.typing():
        raw = await _get_raw()
    drafters, _, _ = _parse(raw)

    pool = sorted(
        [d for d in drafters if d['over'] is not None and d['over'] > 0],
        key=lambda d: d['over'], reverse=True
    )
    below = len([d for d in drafters if d['over'] is not None and d['over'] <= 0])

    top  = '\n'.join(f"{MEDALS[i]} **{d['name']}** — {_over_str(d['over'])}  ({_pct_str(d['pct'])})" for i, d in enumerate(pool[:3]))
    rest = '\n'.join(f"{i:>2}. {d['name']:<22}  {_over_str(d['over']):>6}  {_pct_str(d['pct']):>7}" for i, d in enumerate(pool[3:], 4))
    desc = top + (f"\n```\n{rest}\n```" if rest else "")

    embed = discord.Embed(title=f"✅ Drafters Above .500 ({len(pool)} of {len(drafters)})", description=desc, color=C_GREEN)
    embed.set_footer(text=f"{below} drafter(s) at or below .500")
    await ctx.send(embed=embed)

# ── !drafts ───────────────────────────────────────────────────────────────────

@bot.command(name='drafts')
async def cmd_drafts(ctx):
    if not _in_channel(ctx):
        return

    async with ctx.typing():
        raw = await _get_raw()
    _, draft_numbers, _ = _parse(raw)

    per_row = 6
    rows    = [draft_numbers[i:i + per_row] for i in range(0, len(draft_numbers), per_row)]
    table   = '\n'.join('  '.join(f"D{n:<4}" for n in row) for row in rows)

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

# ── !winhelp ───────────────────────────────────────────────────────────────────

@bot.command(name='winhelp')
async def cmd_winhelp(ctx):
    if not _in_channel(ctx):
        return
    embed = discord.Embed(
        title="📖 ATD Win Bot — Commands",
        description=(
            "`!standings` — Top 20 by total wins\n"
            "`!standings pct` — Top 20 by win% *(min 300 wins)*\n"
            "`!standings recent` — Top 20 in last 5 drafts\n"
            "`!record <name>` — Full all-time record\n"
            "`!ranks <name>` — Where a drafter ranks league-wide\n"
            "`!historys <name>` — Win total per draft, career timeline\n"
            "`!season <num>` — Results for a specific draft\n"
            "`!compare <n1> vs <n2>` — Side-by-side comparison\n"
            "`!above500` — All drafters with a positive record\n"
            "`!active` — Most veteran drafters by drafts played\n"
            "`!drafts` — List all available draft numbers\n"
            "`!winstats` — League-wide highlights\n"
            "`!winhelp` — Show this help"
        ),
        color=C_BLUE
    )
    await ctx.send(embed=embed)

# ── Error handler ──────────────────────────────────────────────────────────────

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
