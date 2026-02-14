@echo off
echo Setting up NBA Team Manager Bot...

REM Create virtual environment
python -m venv venv
call venv\Scripts\activate

REM Install requirements
pip install discord.py gspread oauth2client pandas python-dotenv

REM Create necessary files
if not exist .env (
    echo DISCORD_TOKEN=your_discord_bot_token_here > .env
    echo SPREADSHEET_ID=your_google_sheet_id_here >> .env
)

echo.
echo ✅ Setup complete!
echo.
echo Next steps:
echo 1. Edit .env file with your Discord token and Google Sheet ID
echo 2. Place your service_account.json file in this folder
echo 3. Run: python bot.py
pause