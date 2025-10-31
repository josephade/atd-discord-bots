import os
import re
import discord
import gspread
from dotenv import load_dotenv
from collections import defaultdict
from google.oauth2.service_account import Credentials

# ======================
# 1️⃣ Load environment
# ======================
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
ATD_CHANNEL_ID = int(os.getenv("ATD_RESULTS_CHANNEL_ID"))
SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
CREDENTIALS_PATH = os.getenv("GOOGLE_CREDENTIALS")

# ======================
# 2️⃣ Google Sheets setup
# ======================
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
creds = Credentials.from_service_account_file(CREDENTIALS_PATH, scopes=SCOPES)
gs_client = gspread.authorize(creds)
sheet = gs_client.open_by_key(SHEET_ID)

try:
    drafter_tab = sheet.worksheet("Drafters")
except gspread.exceptions.WorksheetNotFound:
    drafter_tab = sheet.add_worksheet(title="Drafters", rows=1000, cols=3)

# ======================
# 3️⃣ Discord setup
# ======================
intents = discord.Intents.default()
intents.messages = True
intents.message_content = True
intents.guilds = True
intents.members = True  # needed for resolving user mentions

client = discord.Client(intents=intents)

# ======================
# 4️⃣ Regex patterns
# ======================
atd_pattern = re.compile(r"#+\s*ATD\s*(\d+)", re.IGNORECASE)
cancel_pattern = re.compile(r"cancelled|nfl", re.IGNORECASE)

drafter_wins = defaultdict(lambda: {"count": 0, "atds": []})

# ======================
# 5️⃣ Helper: resolve Discord mention IDs to usernames
# ======================
async def resolve_user_name(message, user_id):
    """Converts a Discord mention ID (<@12345>) to a readable display name."""
    try:
        user = await message.guild.fetch_member(int(user_id))
        return user.display_name
    except Exception:
        return f"User_{user_id}"


# ======================
# 6️⃣ Main logic
# ======================
@client.event
async def on_ready():
    print(f"✅ Logged in as {client.user}")
    channel = client.get_channel(ATD_CHANNEL_ID)
    print(f"📜 Reading messages from: #{channel.name} (this may take a while...)")

    messages = []
    async for message in channel.history(limit=None, oldest_first=True):
        if message.content.strip():
            messages.append(message)

    print(f"📦 Total messages fetched: {len(messages)}")

    for message in messages:
        text = message.content
        text = text.replace("–", "-").replace("—", "-")

        # Split into individual ATD blocks
        blocks = re.split(r"(?=#+\s*ATD\s*\d+)", text, flags=re.IGNORECASE)

        for block in blocks:
            block = block.strip()
            if not block:
                continue

            # Find ATD number
            atd_match = atd_pattern.search(block)
            if not atd_match:
                continue

            atd_num = int(atd_match.group(1))

            # Skip cancelled or NFL drafts
            if cancel_pattern.search(block):
                continue

            # Extract the "Winner" section of the block
            winner_section = re.search(r"Winner\s*[-–—:]*\s*(.*?)(?:\n|$)", block, re.IGNORECASE)
            if not winner_section:
                print(f"⚠️ No winner line found for ATD {atd_num}, skipping.")
                continue

            winner_line = winner_section.group(1).strip()

            # Find all mentions in the winner line
            winner_mentions = re.findall(r"<@!?(\d+)>|@([\w .'\-#]+)", winner_line)

            if not winner_mentions:
                print(f"⚠️ No winners detected for ATD {atd_num}, skipping.")
                continue

            # Loop through every mention found (multiple winners supported)
            for uid, uname in winner_mentions:
                if uid:
                    winner = await resolve_user_name(message, uid)
                else:
                    winner = uname.strip()

                drafter_wins[winner]["count"] += 1
                drafter_wins[winner]["atds"].append(f"ATD {atd_num}")

    print("✅ Finished parsing results.")
    print(f"🧾 Found {len(drafter_wins)} total drafters with wins.")

    # ======================
    # 7️⃣ Write to Google Sheet
    # ======================
    print("📤 Writing to Google Sheet (Drafters tab)...")

    data = [["Drafters", "Wins", "ATD"]]
    for name, info in sorted(drafter_wins.items(), key=lambda x: x[1]["count"], reverse=True):
        sorted_atds = sorted(info["atds"], key=lambda s: int(re.search(r'\d+', s).group()))
        data.append([name, info["count"], ", ".join(sorted_atds)])

    drafter_tab.clear()
    drafter_tab.update(values=data, range_name="A1")

    drafter_tab.format('A1:C1', {
        "backgroundColor": {"red": 1, "green": 0, "blue": 1},
        "textFormat": {"bold": True, "foregroundColor": {"red": 0, "green": 0, "blue": 0}},
        "horizontalAlignment": "CENTER"
    })

    total_wins = sum(info["count"] for info in drafter_wins.values())
    print(f"🏆 Total Wins Counted: {total_wins}")
    print("✅ Drafter data written successfully!")
    await client.close()


# ======================
# 8️⃣ Run bot
# ======================
if __name__ == "__main__":
    client.run(TOKEN)
