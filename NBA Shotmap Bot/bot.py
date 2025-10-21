import os
import discord
from discord.ext import commands
from dotenv import load_dotenv
from shotmap_bot import generate_all_shotmaps

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

if not TOKEN:
    print("❌ No DISCORD_TOKEN found. Add it to your .env file.")
    exit(1)

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="$", intents=intents)

@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")
    print("Listening for commands...")

@bot.command()
async def shotmap(ctx, *, args: str):
    """Usage:
       $shotmap lebron james 2024
       $shotmap lebron james 2024 ps
    """
    print(f"📩 Command: {args}")
    await ctx.send("🕐 Processing your request...")

    try:
        parts = args.split()
        playoffs = parts[-1].lower() == "ps"
        year = parts[-2] if playoffs else parts[-1]
        name_parts = parts[:-2] if playoffs else parts[:-1]
        player_name = " ".join(name_parts)

        await ctx.send(f"📸 Generating 4-shotmap pack for **{player_name} {year} {'Playoffs' if playoffs else 'Regular Season'}**...")

        paths, pid = generate_all_shotmaps(player_name, year, playoffs)

        headshot_url = f"https://ak-static.cms.nba.com/wp-content/uploads/headshots/nba/latest/260x190/{pid}.png"
        embed = discord.Embed(
            title=f"{player_name.title()} | {year} {'Playoffs' if playoffs else 'Regular Season'}",
            description="Shot maps generated from official NBA data.",
            color=0x00FFAA
        )
        embed.set_thumbnail(url=headshot_url)

        await ctx.send(embed=embed, files=[discord.File(p) for p in paths])

    except Exception as e:
        print(f"❌ Error: {e}")
        await ctx.send(f"⚠️ Error: {e}")

bot.run(TOKEN)
