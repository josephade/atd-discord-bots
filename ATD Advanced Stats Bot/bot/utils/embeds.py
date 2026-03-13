"""
Embed builders — one function per command type.
Kept separate so cogs stay thin.
"""

import discord


def stats_embed(d: dict) -> discord.Embed:
    embed = discord.Embed(
        title=f"📊 {d['name']} — {d['season']}",
        description=f"**{d['team']}** | {d['position']} | Age {d['age']}",
        color=0x1D428A,
    )
    embed.add_field(name="PTS",  value=d["pts"],           inline=True)
    embed.add_field(name="REB",  value=d["reb"],           inline=True)
    embed.add_field(name="AST",  value=d["ast"],           inline=True)
    embed.add_field(name="STL",  value=d["stl"],           inline=True)
    embed.add_field(name="BLK",  value=d["blk"],           inline=True)
    embed.add_field(name="TOV",  value=d["tov"],           inline=True)
    embed.add_field(name="FG%",  value=f"{d['fg_pct']}%",  inline=True)
    embed.add_field(name="3P%",  value=f"{d['fg3_pct']}%", inline=True)
    embed.add_field(name="FT%",  value=f"{d['ft_pct']}%",  inline=True)
    embed.add_field(name="TS%",  value=f"{d['ts_pct']}%",  inline=True)
    embed.add_field(name="USG%", value=f"{d['usg_pct']}%", inline=True)
    embed.add_field(name="MPG",  value=d["min"],           inline=True)
    embed.add_field(name="GP",   value=d["gp"],            inline=True)
    embed.add_field(name="+/-",  value=d["plus_minus"],    inline=True)
    embed.set_footer(text="Source: NBA Stats API • databallr-style")
    return embed


def onoff_embed(d: dict, player: str) -> discord.Embed:
    embed = discord.Embed(
        title=f"🔛 On/Off Splits — {player.title()}",
        description=f"**{d['team']}** | {d['season']}",
        color=0xFDB927,
    )
    embed.add_field(
        name="📈 With Player ON Court",
        value=(
            f"NetRtg: **{d['on_net']}**\n"
            f"OffRtg: {d['on_off_rtg']} | DefRtg: {d['on_def_rtg']}\n"
            f"MIN: {d['on_min']}"
        ),
        inline=False,
    )
    embed.add_field(
        name="📉 With Player OFF Court",
        value=(
            f"NetRtg: **{d['off_net']}**\n"
            f"OffRtg: {d['off_off_rtg']} | DefRtg: {d['off_def_rtg']}\n"
            f"MIN: {d['off_min']}"
        ),
        inline=False,
    )
    try:
        diff  = round(float(d["on_net"]) - float(d["off_net"]), 1)
        emoji = "🟢" if diff > 0 else "🔴"
        embed.add_field(
            name="Net Differential (On − Off)",
            value=f"{emoji} **{diff:+.1f}**",
            inline=False,
        )
    except Exception:
        pass
    embed.set_footer(text="Source: NBA Stats API • databallr-style")
    return embed


def wowy_embed(d: dict, p1: str, p2: str) -> discord.Embed:
    embed = discord.Embed(
        title=f"🤝 WOWY — {p1.title()} & {p2.title()}",
        description=(
            f"How **{p1.title()}** performs with/without **{p2.title()}** "
            f"| {d.get('season', '')}"
        ),
        color=0x00A36C,
    )
    embed.add_field(
        name="✅ Together (Both ON)",
        value=(
            f"NetRtg: **{d['both_on_net']}**\n"
            f"OffRtg: {d['both_on_off']} | DefRtg: {d['both_on_def']}\n"
            f"MIN: {d['both_on_min']}"
        ),
        inline=False,
    )
    embed.add_field(
        name=f"🔵 {p1.title()} ON / {p2.title()} OFF",
        value=(
            f"NetRtg: **{d['p1_on_net']}**\n"
            f"OffRtg: {d['p1_on_off']} | DefRtg: {d['p1_on_def']}\n"
            f"MIN: {d['p1_on_min']}"
        ),
        inline=False,
    )
    embed.add_field(
        name="🔴 Both OFF",
        value=(
            f"NetRtg: **{d['both_off_net']}**\n"
            f"OffRtg: {d['both_off_off']} | DefRtg: {d['both_off_def']}\n"
            f"MIN: {d['both_off_min']}"
        ),
        inline=False,
    )
    embed.set_footer(text="Source: NBA Stats API • databallr-style")
    return embed


def lastx_embed(d: dict, player: str, games: int) -> discord.Embed:
    embed = discord.Embed(
        title=f"🕐 Last {games} Games — {player.title()}",
        description=f"**{d['team']}** | {d['season']}",
        color=0xC8102E,
    )
    embed.add_field(name="PTS", value=d["pts"],           inline=True)
    embed.add_field(name="REB", value=d["reb"],           inline=True)
    embed.add_field(name="AST", value=d["ast"],           inline=True)
    embed.add_field(name="FG%", value=f"{d['fg_pct']}%",  inline=True)
    embed.add_field(name="3P%", value=f"{d['fg3_pct']}%", inline=True)
    embed.add_field(name="FT%", value=f"{d['ft_pct']}%",  inline=True)
    embed.add_field(name="STL", value=d["stl"],           inline=True)
    embed.add_field(name="BLK", value=d["blk"],           inline=True)
    embed.add_field(name="TOV", value=d["tov"],           inline=True)
    embed.add_field(name="TS%", value=f"{d['ts_pct']}%",  inline=True)
    embed.add_field(name="+/-", value=d["plus_minus"],    inline=True)
    embed.add_field(name="MPG", value=d["min"],           inline=True)
    if d.get("game_log"):
        lines = [
            f"`{g['date']} vs {g['matchup'][-3:]}` — "
            f"{g['pts']}pts / {g['reb']}reb / {g['ast']}ast"
            for g in d["game_log"][:5]
        ]
        embed.add_field(name="Recent Games", value="\n".join(lines), inline=False)
    embed.set_footer(text="Source: NBA Stats API • databallr-style")
    return embed


def team_embed(d: dict) -> discord.Embed:
    embed = discord.Embed(
        title=f"🏀 {d['name']} — {d['season']}",
        description=(
            f"Record: **{d['wins']}-{d['losses']}** "
            f"| {d['conf']} | Rank: #{d['conf_rank']}"
        ),
        color=0x552583,
    )
    embed.add_field(name="OffRtg",    value=d["off_rtg"],         inline=True)
    embed.add_field(name="DefRtg",    value=d["def_rtg"],         inline=True)
    embed.add_field(name="NetRtg",    value=d["net_rtg"],         inline=True)
    embed.add_field(name="Pace",      value=d["pace"],            inline=True)
    embed.add_field(name="eFG%",      value=f"{d['efg_pct']}%",  inline=True)
    embed.add_field(name="TS%",       value=f"{d['ts_pct']}%",   inline=True)
    embed.add_field(name="TOV%",      value=f"{d['tov_pct']}%",  inline=True)
    embed.add_field(name="OREB%",     value=f"{d['oreb_pct']}%", inline=True)
    embed.add_field(name="FT Rate",   value=d["ft_rate"],         inline=True)
    embed.add_field(name="PTS/G",     value=d["pts"],             inline=True)
    embed.add_field(name="OPP PTS/G", value=d["opp_pts"],         inline=True)
    embed.add_field(name="3PA/G",     value=d["fg3a"],            inline=True)
    embed.set_footer(text="Source: NBA Stats API • databallr-style")
    return embed