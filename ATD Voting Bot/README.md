# ATD Voting Bot

Keep posting the same Google Form in announcements. Each new response becomes a
personal tier-list PNG sent by Discord DM. Team voting reports are available on
demand; the bot never writes to the response Sheet.

## Local setup

Run this bot in the foreground of your terminal only. Do not start hidden or
detached background processes. From the workspace root in PowerShell:

```powershell
Set-Location 'ATD Voting Bot'
.\.venv\Scripts\python.exe -u bot.py
```

Keep that terminal open while collecting votes. Press Ctrl+C to stop the bot;
closing the terminal also stops it. Do not automatically restart it in the background.

Use Python 3.11 or later. From this directory, in PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Set the bot token and ATD server ID in `.env`. In the Discord Developer Portal,
enable **Server Members Intent** and **Message Content Intent**. Invite this bot
to the server with View Channels, Send Messages, and Attach Files permissions.
Use a private commissioner channel for `ADMIN_CHANNEL_ID` if desired.

Place your Google service account file at `service_account.json`, or set
`SERVICE_ACCOUNT_FILE` to its local path. `GOOGLE_SERVICE_ACCOUNT_JSON` is also
supported. Share the response spreadsheet with the service account as **Viewer**
and enable the Google Sheets API in its Google Cloud project. Credentials must not
be committed.

The response header must contain `Timestamp` (or `Tidsmerke`), `Enter Discord Name`,
and team columns such as `[Denver Nuggets]` or `Rate teams [Denver Nuggets]`.
Set `NAME_COLUMN` if the form uses another exact question title. Ratings may be
`Tier 1` or `1`. Copied tables, summaries, and derived columns are ignored.

## Test locally before deployment

Offline preview (no Google or Discord connection):

```powershell
.\.venv\Scripts\python.exe bot.py --preview "C:\Users\josep\Downloads\ATD 106 Seeding (Responses) - Skjemasvar 1.csv" --draft 106 --tiers 12
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

For a connected test, use a **copy of the voting form and its response Sheet**,
leaving `DRY_RUN=true` initially. Start `python bot.py`, then register that Sheet:

```text
!votetrack 112 https://docs.google.com/spreadsheets/d/SHEET_ID/edit#gid=TAB_ID 10
```

Link URLs without `gid` use the first tab, validate its response headers, and save
that tab's actual ID. For another tab, copy its URL with `gid` included.

Registration marks existing ballots historical without sending them. Submit a
new vote using your Discord name. Within the polling interval, the preview should
appear in `state/previews/`; the log shows the matched member ID. Check the image
and recipient. Add an alias if your form name differs from your Discord name:

```text
!votealias @Soapz Soapz
```

Exact case-insensitive username, global name, nickname, or saved alias matches are
supported; no fuzzy matches. Duplicate matches remain unmatched until mapped.
Explicit Discord IDs/mentions are also accepted. A typed form name is not proof
of identity; anyone can enter someone else's name. Use this for ordinary tier-list
receipts, not verified anonymous voting or authentication.

Stop the bot, set `DRY_RUN=false`, and restart **with the same state directory**
when ready for a real local DM test. Dry-run pending ballots are still eligible
for delivery (up to five minutes after their last preview). Test a restart after
delivery to confirm it does not resend. Test closed DMs and retry with
`!voteretry 112` once DMs are enabled. No deployment is included.

## Commands and delivery behavior

Voter commands (`!mytierlist`, `!tierlist`, and `!votehelp`) are available in ATD channels
`934052158532378634`, `1471629828208988314`, `934054851485270016`, and `1533242104304631969`, or in DMs. Commissioner commands
remain available throughout the configured ATD server.

To import an old draft for on-demand receipts without watching it or sending DMs:

```powershell
.\.venv\Scripts\python.exe import_history.py 111 "https://docs.google.com/spreadsheets/d/SHEET_ID/edit#gid=TAB_ID" --tiers 10
```

Imports validate the original ballots and save them as historical with polling
disabled. Existing draft registrations are left untouched. Voters can then use
`!mytierlist 111`; commissioners can check names with `!votematches 111`.

- `!votestatus`: counts by draft and delivery status.
- `!tierlist [draft] <team emoji or name>`: show the team's tier distribution,
  average, median and every voter's selection on one full-resolution PNG.
  The report uses the team's logo and colours, groups names by tier, and highlights
  standout votes. No pagination or page buttons. Server team emojis provide logos
  (including college/historic teams), with ESPN NBA logos as a fallback.
  Typed `:lakers:`, Discord custom emojis, full names and NBA abbreviations work.
  The default is the highest numbered active draft (currently 113).
  Use `!tierlist 113 :PORF:` or test historical data with `!tierlist 112 :celtics:`
  (Lakers are not on the 112 ballot). The draft goes before the team.
  Each report reads the current linked Google Form response sheet. Deleted rows and
  superseded selections are excluded from the voters, distribution, averages and
  standout calculation. Archived delivery records are never merged into reports.
  If Google cannot be read, the command shows an error instead of old votes.
  Reports do not change delivery status, tracking or send DMs.
  The latest valid ballot per exact normalized name or resolved Discord identity is used.
  Standouts are votes outside Q1–Q3 by more than `max(2, 1.5 × IQR)` tiers,
  with at least five voters. Both unusually high and low ratings can be flagged.
  This is a robust consensus rule rather than distance from the average alone.
  Rerun the command for a fresh snapshot immediately, without waiting for the poll.
- `!votematches <draft>`: check every saved form name, including historical votes,
  and list names that need an alias. Rerun after adding aliases to confirm matches.
- `!voteissues <draft>`: list unmatched names, invalid ballots, and failed deliveries.
- `!votepause <draft>` / `!voteresume <draft>`: stop/resume watching a draft.
- `!votealias @member <form name>`: save an explicit recipient mapping.
- `!voteretry <draft>`: retry blocked, unmatched, or retryable deliveries.
- `!mytierlist <draft>`: show your most recently saved matching ballot in the
  channel or DM where you run the command, including
  historical votes from before registration. Exact names/aliases must match your
  own account. Explicit requests show the image even when automatic delivery is
  in dry run. Historical records remain historical and are not automatically queued.

Commissioner commands require server Administrator or the `LeComissioner` role
in the configured server. A missing or invalid team rating prevents automatic
delivery; its record is marked `invalid`. Correcting a submitted ballot creates
a new revision and image. Exact repeated submissions with identical timestamps,
names, and tiers are deduplicated. Different timestamps are separate submissions.
Keep the original response table first. A repeated Timestamp header marks the
start of a copied/formula table; everything after it is ignored. Do not rearrange
the original responses into ranking tables.

SQLite stores tracked drafts, ballots, aliases, and delivery status under
`STATE_DIR`. Keep it on persistent storage and run only **one bot process** per
database. Transient Google failures are retried on the next poll. Unmatched names
are checked again every five minutes. Closed DMs require an explicit retry.

The normal `bot.py` entry point holds an OS lock in `state/bot.lock` for this
installation; a second launch exits before connecting to Discord or polling votes.
The lock is released automatically when the process exits.

Messages interrupted during sending or with ambiguous server errors are marked
`uncertain` and are not resent automatically; inspect delivery before intervening.
Discord and SQLite cannot guarantee exactly-once delivery across a crash.

The tier count is configurable (10 by default); higher selected tiers are always
included. Images adapt to 24, 30, or more team columns and keep ties together.
