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
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))
SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
CREDENTIALS_PATH = os.getenv("GOOGLE_CREDENTIALS")

# ======================
# 2️⃣ Google Sheets setup
# ======================
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
creds = Credentials.from_service_account_file(CREDENTIALS_PATH, scopes=SCOPES)
gs_client = gspread.authorize(creds)
sheet = gs_client.open_by_key(SHEET_ID).sheet1

# ======================
# 3️⃣ Discord setup
# ======================
intents = discord.Intents.default()
intents.messages = True
intents.message_content = True

client = discord.Client(intents=intents)

# ======================
# 4️⃣ Player regex pattern
# ======================
player_pattern = re.compile(
    r"\b([A-Z][a-zA-Z'\.]+(?: [A-Z][a-zA-Z'\.]+)+)\b"
)

player_counts = defaultdict(int)

# ======================
# 5️⃣ Helper: normalize names
# ======================
def normalize_name(name: str) -> str:
    """Normalize capitalization & punctuation for consistent counting."""
    name = name.strip().lower()

    # Normalize common characters
    name = name.replace("’", "'").replace("`", "'").replace("´", "'")

    # Title case (LeBron James, Amar'e Stoudemire)
    name = " ".join([w.capitalize() if len(w) > 2 else w for w in name.split()])
    return name


# ======================
# 6️⃣ Bot logic
# ======================
@client.event
async def on_ready():
    print(f"✅ Logged in as {client.user}")
    channel = client.get_channel(CHANNEL_ID)
    print(f"📜 Reading messages from: #{channel.name} (this may take a while...)")

    async for message in channel.history(limit=None, oldest_first=True):
        lines = message.content.splitlines()
        for line in lines:
            # Only count lines with years (team posts)
            if not re.search(r"\d{4}", line):
                continue

            matches = player_pattern.findall(line)
            for name in matches:
                if len(name.split()) in [2, 3]:
                    normalized = normalize_name(name)
                    player_counts[normalized] += 1

    print(f"✅ Finished counting {len(player_counts)} unique players.")

    # ======================
    # 7️⃣ Write to Google Sheet (batch)
    # ======================
    print("📤 Writing to Google Sheet (batch mode)...")

    sorted_data = sorted(player_counts.items(), key=lambda x: x[1], reverse=True)
    data = [["Player", "Wins"]] + [[n, c] for n, c in sorted_data]

    sheet.clear()
    sheet.update("A1", data)

    # Optional: style header row
    sheet.format('A1:B1', {
        "backgroundColor": {"red": 1, "green": 0, "blue": 1},
        "textFormat": {"bold": True, "foregroundColor": {"red": 0, "green": 0, "blue": 0}},
        "horizontalAlignment": "CENTER"
    })

    print("✅ Google Sheet updated successfully!")
    await client.close()

# ======================
# 8️⃣ Run bot
# ======================
if __name__ == "__main__":
    client.run(TOKEN)
