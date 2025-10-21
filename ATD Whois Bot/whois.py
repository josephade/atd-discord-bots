import discord
from discord.ext import commands
from player_data import get_player_data
from dotenv import load_dotenv
import os
import re

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="?", intents=intents)

@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")

@bot.command()
async def whois(ctx, *, query: str):
    # --- Detect year range ---
    year_match = re.search(r"(19|20)\d{2}\s*[-–]?\s*(\d{2,4})?", query)
    if year_match:
        full = year_match.group(0).replace("–", "-").strip()
        if re.match(r"(19|20)\d{2}-\d{2}$", full):
            start_year = full.split("-")[0]
            end_short = full.split("-")[1]
            end_year = str(int(start_year[:2] + end_short))
            year_range = f"{start_year}-{end_year}"
        else:
            year_range = full
        player_name = re.sub(re.escape(full), "", query).strip()
    else:
        player_name = query.strip()
        year_range = None

    player = get_player_data(player_name)
    if not player:
        await ctx.send(f"❌ Couldn’t find info for **{player_name}**.")
        return

    # --- Choose the correct stats ---
    if year_range and year_range in player["season_stats"]:
        stats = player["season_stats"][year_range]
        metrics = player["impact_metrics"].get(year_range, player["impact_metrics"]["prime"])
        shotmap_url = f"https://nbavisuals.com/shotmap?player={player['name'].replace(' ', '%20')}&season={year_range}"
        display_years = year_range
    else:
        stats = player["prime_stats"]
        metrics = player["impact_metrics"]["prime"]
        shotmap_url = player["shotmap"]
        display_years = player["prime_years"]

    ref_link = player["bref"]
    apm_link = player["nbarapm"]

    # --- Build Embed ---
    embed = discord.Embed(
        title=f"{player['name']}",
        description=f"🏀 **Years Analyzed:** {display_years}",
        color=0x2ecc71,
    )

    embed.set_thumbnail(url=player["image"])

    embed.add_field(
        name="📊 Stats Summary",
        value=(
            f"**PPG:** {stats['ppg']}\n"
            f"**RPG:** {stats['rpg']}\n"
            f"**APG:** {stats['apg']}\n"
            f"**FG%:** {stats['fg']}%\n"
            f"**3P%:** {stats['three_pt']}%  |  **3PA:** {stats['three_pa']}\n\n"
            f"[📎 View Full Stats on Basketball Reference]({ref_link})"
        ),
        inline=False,
    )

    embed.add_field(name="⚔️ Offensive Breakdown", value=player["offense"], inline=False)
    embed.add_field(name="🛡️ Defensive Breakdown", value=player["defense"], inline=False)

    embed.add_field(
        name="📈 Impact Metrics",
        value=(
            f"**RAPTOR:** Off +{metrics['raptor']['off']} | Def +{metrics['raptor']['def']} | "
            f"Total +{metrics['raptor']['total']} (Rank: #{metrics['raptor']['rank']})\n"
            f"**LEBRON:** Off +{metrics['lebron']['off']} | Def +{metrics['lebron']['def']} | "
            f"Total +{metrics['lebron']['total']} (Rank: #{metrics['lebron']['rank']})\n"
            f"**DARKO:** Off +{metrics['darko']['off']} | Def +{metrics['darko']['def']} | "
            f"Total +{metrics['darko']['total']} (Rank: #{metrics['darko']['rank']})\n\n"
            f"[📊 More Advanced Metrics (nbarapm.com)]({apm_link})"
        ),
        inline=False,
    )

    # embed.set_image(url=shotmap_url)

    embed.set_footer(
        text="ATD Whois Bot • Prime Performance Analytics",
        icon_url="https://cdn-icons-png.flaticon.com/512/124/124010.png",
    )

    await ctx.send(embed=embed)


if TOKEN:
    bot.run(TOKEN)
else:
    print("❌ ERROR: DISCORD_TOKEN not found in environment variables.")
