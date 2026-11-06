Use [README.md](README.md) for the normal one-command setup and button panel. `!simcreate ATD <number>` now performs automatic setup; the walkthrough below is only for custom manual tournaments. Live lineup order is no longer frozen at bracket creation.

# Optional advanced/manual controls

A separate Discord application for ATD playoff games. Voting odds decide the
winner. Basketball scores and player stat lines are generated to explain that
result; they are synthetic, not historical NBA results or a predictive model.

## Current readiness

Code, tests, full-resolution PNG cards, Docker image definition and Fly config
are included. The local `.env` holds this application's private settings.
The current instance is deployed as `atd-playoff-bot` on Fly using the separate
**ATD Sim Bot** Discord account. No other bot's token is reused.
For a fresh installation, supply a new application token before connecting.

## Discord setup

1. Create a new Discord application named **ATD Playoff Bot** and its bot user.
2. Enable **Message Content Intent** in the Developer Portal. Member/Presence
   privileged intents are not required.
3. Invite with the `bot` scope and View Channels, Send Messages, Embed Links,
   Attach Files and Read Message History permissions. Application command
   scope is optional; these are `!` prefix commands, not slash commands.
4. Put its token in `.env` as `DISCORD_TOKEN=...`. Set `DISCORD_GUILD_ID` for
   predictable DM voting. Never post the token in Discord or chat.
5. Owner ID defaults to `681833601830354944`. Admin setup channel defaults to
   `1533242104304631969`; hide it from @everyone. The owner, members with Manage
   Server/Administrator, and `COMMISH_ROLE_ID` can administer the bot, but setup
   is still restricted to the private channel. Only game simulation and focus
   selection are allowed in public playoff channels.
6. Set optional `PUBLIC_CHANNEL_IDS` to restrict command channels, and
   `VOTER_ROLE_ID` to restrict ballots. An empty voter role allows every server
   member, not only registered ATD GMs. GM identities are not stored or displayed.

## Run locally

Use Python 3.12 or newer from this directory:

```powershell
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
.venv/Scripts/python -m unittest discover -s tests -v
.venv/Scripts/python bot.py
```

Local state lives in `data/playoffs.sqlite3`. Closing that terminal ends the
local process. Do not run locally with the same token while Fly is running.

## Fly hosting

`fly.toml` is configured for one 512 MB machine in `ewr`, an immediate deployment
and a persistent `/data` volume. No HTTP service is needed for a Discord bot.

Run from this directory after filling `.env`:

```powershell
fly auth login
fly apps create atd-playoff-bot
fly volumes create atd_playoff_data --app atd-playoff-bot --region ewr --size 1
.venv/Scripts/python deploy.py
fly logs --app atd-playoff-bot
```

If the app name is unavailable, choose another and update `fly.toml`. `deploy.py`
reads that name, checks for the persistent volume, imports secrets over stdin
and deploys. It never prints tokens or includes `.env`/Google credentials in
the image. Fly hosting incurs the normal machine and volume charges.

Keep exactly **one machine** attached to the SQLite volume. Never scale this
bot to multiple independent volumes: each would have a different game ledger.
The Docker build runs all tests before replacing a running machine.

For roster import, share the Google Sheet with your service account (Viewer is
enough). Set `SERVICE_ACCOUNT_FILE` to its local JSON path; `deploy.py` uploads
the file as `GOOGLE_SERVICE_ACCOUNT_B64`, a Fly secret (base64 avoids multiline
JSON escaping issues in the CLI). Raw `GOOGLE_SERVICE_ACCOUNT_JSON` is also
supported for manual secret setup. Without credentials,
manual roster entry and all simulations still work. Google Sheet access is
read-only; no prices, player years or results are written back.

## First tournament: commissioner walkthrough

All setup below is in the private admin channel.

1. `!simcreate Manual Playoffs` (a custom name without an ATD number keeps manual setup)
2. Register every playoff team by **regular-season seed**, 1 through 16:
   `!simteam 1 :Bullets: Baltimore Bullets`.
3. Set each ten-player roster. Either use manual entry below, or configure
   `!simsheet https://docs.google.com/spreadsheets/d/ID/edit | ATD 114 - Anon Budget`
   and run `!simimport 1` through `!simimport 16`. Team names must match sheet
   headers exactly. Roster rows are PG/SG/SF/PF/C, then five bench slots, with
   season in the adjacent Year column.
4. Run `!simloadstats <seed>` for exact-season NBA baselines, or enter
   season averages with `!simprofile`. The roster
   sheet has names/seasons/prices, **not** season stat averages. Until supplied,
   the bot uses and labels estimated positional scoring/rebounding/assist
   baselines. A season label alone does not fetch historical stats. Bench
   minute allocations are smaller than starter allocations.
5. `!simbracket` creates the eight first-round series and fixes the eligible player names. Starting/bench order stays live in Google Sheets.
   Opening slots are 1v16, 8v9, 4v13, 5v12, 2v15, 7v10, 3v14, 6v11. Winners
   of adjacent slots meet next; this is a global seeded bracket, not separate
   Eastern/Western conference brackets. The higher regular-season seed always
   gets HCA in every round.
6. For each series enter your **official existing voting-sheet odds**:
   `!simodds 1 47` means the higher seed has 47%, the lower seed 53%, before
   HCA or deductions. This is the recommended route for your current process.
   Alternatively use the bot's own ballots, described below. The bot does not
   silently read or assume your existing voting formula.
7. Apply a known suspect penalty, if any: `!simpenalty 1 1 10`. This sets a
   10-percentage-point deduction on seed 1 for this entire series, not only
   Game 1. Repeating it does not stack. `0` corrects it before Game 1. Theme
   Bot's ledger is not automatically consumed; commissioners transfer each
   applicable next-matchup deduction once. Advancing to another round starts
   with no deductions unless the commissioner explicitly enters a new one.
8. In the intended public playoff channel, `!simfocus 1` selects Series 1.
9. `!sim Game 1`, then `!sim Game 2`, and so on. An explicit series ID also
   works: `!sim Game 1 1`. The bot stops at four wins and saves every result.
10. Once **every series** in the round is finished, run `!simadvance` privately.
    New series IDs are shown. Collect new matchup votes, enter new odds and
    simulate the next round. Repeat through the Finals.
11. `!playoffs` announces the champion in its bracket report. Export the private
    audit with `!simexport`, then `!simarchive` before creating the next ATD.

Manual roster example (exactly ten lines, first five are starters):

```text
!simsetroster 1
Mark Price | 1991-92
Donovan Mitchell | 2023-24
Mikal Bridges | 2021-22
Karl Malone | 1997-98
Bill Walton | 1976-77
Sam Cassell | 2003-04
Ron Harper | 1988-89
Caron Butler | 2007-08
Robert Horry | 1995-96
Bob Pettit | 1958-59
```

`!simprofile 1 4 27 10.3 3.9` enters a scoring/rebounding/assist baseline for
seed 1's fourth slot. These are commissioner inputs, not verified historical
statistics. Each is a distribution weight; a 30 PPG baseline does not guarantee
30 generated points per game in an all-time roster. Baselines cannot affect
who wins or alter the voting odds. No individual-player pace or defensive
matchup model is claimed in version 1. The optional NBA import also weights
three-point allocation using the selected season's three-point makes / field-goal
makes. Recorded zero-share players do not receive three-point attempts.
Individual shooting percentages remain synthetic.

`!simloadstats <seed>` uses exact normalized player names and exact NBA
regular-season rows from the
[PlayerCareerStats endpoint](https://github.com/swar/nba_api/blob/master/docs/nba_api/stats/endpoints/playercareerstats.md).
Multi-team seasons use the total row. Career responses are cached on the
persistent volume. Missing names/seasons and NBA service failures leave existing
baselines intact and are reported privately. ABA-only seasons require manual
entry. This import is optional setup work, never a dependency of game simulation.
Cached career rows are retained; this is not a live-season statistics feed.

## Optional built-in series voting

GMs may DM the new bot `!simvote <series ID> <choice>`, or run it in an allowed
server channel. DM is preferable if ballots should remain private. Server
command messages themselves are visible to other members in that channel.
Each Discord user has one ballot per series; the latest replaces the earlier
one until voting closes. Eligible voter role can be configured.

| Choice | Prediction |
|---|---|
| 0 | Higher seed wins 4-0 |
| 1 | Higher seed wins 4-1 |
| 2 | Higher seed wins 4-2 |
| 3 | Higher seed wins 4-3 |
| 4 | Lower seed wins 4-3 |
| 5 | Lower seed wins 4-2 |
| 6 | Lower seed wins 4-1 |
| 7 | Lower seed wins 4-0 |

`!simclosevotes <ID>` derives a **per-game** probability as the sum of the
higher seed's predicted game wins divided by all predicted games, across
ballots. For example a single higher-seed-in-7 ballot yields 4/7 = 57.14%.
This is an explicit optional model, **not** a claim about your Google Form's
formula and not a series-win probability. Use `!simodds` for official sheet
odds instead. Both routes close voting. Odds, ballots and deductions lock at
Game 1. `!series` shows the probability source and next game's final odds.

## Odds, score and result rules

- Games 1/2/5/7: higher seed is home. Games 3/4/6: lower seed is home.
- HCA is **10 percentage points**, not a 1.10 multiplier.
- Higher seed's game chance = base higher-seed odds + home adjustment
  (plus 10 when home, minus 10 when away) - its penalty + opponent's penalty.
  Clamp to 0-100%; probabilities always sum to 100%.
- 47/53 with the higher seed at home becomes **57/43**. A 10-point penalty
  to that home team makes it **47/53**. Both teams penalized cancels out.
- The saved random roll is uniformly 1-10000, preserving hundredths of a
  percent. Higher seed wins when roll <= its probability in basis points.
- The winner is selected first. Score, quarter totals and player statistics
  are generated consistently with that outcome. This is a basketball story
  around the odds, not a possession-level basketball physics simulation.
- Cards show points/rebounds/assists, steals, blocks, turnovers, minutes and
  made-attempted FG/3PT/FT. Points exactly match shooting and the team score.
  Team minutes total 240 (265 for overtime); tied regulation precedes OT.
- `!seriesstats` calculates averages from saved simulated games only.
- Game 2 cannot precede Game 1. A repeated game returns its saved result,
  including after a restart. Concurrent requests cannot generate two results.
  There is no reroll/delete-played-game command. A Discord attachment failure
  cannot undo a saved game; retrieve it with `!boxscore`.
- A cryptographic random seed, roll, engine version, inputs, timestamp and
  acting commissioner are retained. `!simaudit` reproduces the result using
  the current engine. Preserve engine version 1.0 for future reproduction.
- Eligible player names are fixed at bracket creation. Starting lineups refresh before every new game, and each saved game retains its own lineup snapshot. Odds/deductions lock at first game.
  `!simresetbracket CONFIRM` can clear **only an entirely unplayed bracket**,
  including its ballots, to correct setup. Once any game exists it refuses.

## Every admin command

`ID` means **series ID**, not ATD/draft ID. `seed` means regular-season seed.

| Command | Purpose | Where |
|---|---|---|
| `!simcreate <name>` | Create the active ATD playoffs | Private admin |
| `!simtest <name>` | Create a labelled test; can archive it at any point | Private admin |
| `!simteam <seed> <emoji> <team name>` | Register/update a seed before bracket | Private admin |
| `!simsetroster <seed>` + 10 `Name \| Season` lines | Save roster | Private admin |
| `!simsheet <URL or ID> \| <tab>` | Configure read-only roster sheet | Private admin |
| `!simimport <seed>` | Import that team's ten players/seasons | Private admin |
| `!simprofile <seed> <slot> <PPG> <RPG> <APG>` | Set baseline, slots 1-10 | Private admin |
| `!simloadstats <seed>` | Attempt exact-season NBA baselines | Private admin |
| `!simbracket` | Validate all 16 teams, create Round 1 | Private admin |
| `!simresetbracket CONFIRM` | Clear unplayed bracket/ballots for corrections | Private admin |
| `!simodds <ID> <higher-seed percent>` | Enter official odds, close voting | Private admin |
| `!simclosevotes <ID>` | Derive odds from built-in ballots | Private admin |
| `!simpenalty <ID> <seed> [10 or 0]` | Set/remove next-series suspect deduction | Private admin |
| `!simfocus <ID>` | Choose channel's default series | Allowed server channel |
| `!sim Game <1-7> [ID]` | Simulate next game, or show saved game | Allowed server channel |
| `!simadvance` | Create next round after all series finish | Private admin |
| `!simaudit <ID> <game>` | Show saved roll, seed and reproduction check | Private admin |
| `!simexport` | Export saved results/ballots/audit JSON | Private admin |
| `!simarchive` | Archive after Finals; preserve games | Private admin |

## Every GM command

| Command | Purpose |
|---|---|
| `!playoffs` | Bracket, series scores and champion |
| `!series [ID]` | Matchup, voting odds, HCA, deductions and game results |
| `!simvote <ID> <0-7>` | Save/replace your prediction; DM supported |
| `!boxscore <game> [ID]` | Saved scorecard with buttons for both box scores and averages |
| `!boxscores <game> [ID]` | Alias of `!boxscore` |
| `!seriesstats [ID]` | Both rosters' series averages |
| `!simroster <seed>` | Players/seasons without GM identities |
| `!simhelp` | In-Discord command guide (admins see admin commands too) |

When ID is omitted, the command uses the channel's `!simfocus`. In DMs supply
the ID. Existing series/game/stat commands can retrieve archived series by ID.
Buttons are conveniences with a 15-minute lifetime; after timeout/restart,
rerun `!boxscore` to get fresh buttons. Buttons open individual private pages,
so one viewer cannot change another's page. All images are sharp PNGs, not GIFs.

## Verification

Run `python -m unittest discover -s tests -v`. Tests cover accounting across
250 generated games, overtime, fixed-seed reproduction, odds/HCA/deductions,
ballots, duplicate concurrent simulations, restart persistence, no-reroll
guards, complete 16-team progression through a champion, archival, cross-server
isolation, admin permissions, roster import and all PNG page types.

Test seasons (`!simtest ATD 113`) carry TEST labels on all game reports. `!simarchive` may end them mid-series without deleting saved games. Normal tournaments still require the Finals to finish before archiving. Test results never write back to the roster/voting sheets.
