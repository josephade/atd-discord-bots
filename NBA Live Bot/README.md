# NBA Live Bot

A standalone Discord bot for real NBA games. It creates a dark PNG stat card with a player headshot, team, score, game clock, points, threes, shooting percentages, rebounds, assists, steals, blocks, plus/minus, turnovers, fouls, offensive/defensive rebounds, true shooting, and fantasy points.

## Setup

Use Node.js 22 or later. In PowerShell:

```powershell
cd 'NBA Live Bot'
npm.cmd ci
Copy-Item .env.example .env
```

Set `DISCORD_TOKEN` in `.env` to the token for a new Discord application/bot. Enable **Message Content Intent** in the Discord Developer Portal. Invite it with **View Channels**, **Send Messages**, **Read Message History**, and **Attach Files** permissions. Optionally set `GUILD_ID` and `ALLOWED_CHANNEL_IDS` to limit access. Then:

```powershell
npm.cmd start
```

## Commands

| Command | Result |
| --- | --- |
| `!nba injury` | All listed NBA injuries, paginated, with team selectors and a Refresh button |
| `!nba injury Lakers` | Open a team's report directly; full names, unique partial names, abbreviations and team IDs work |
| `!nba injury :lakers:` | Open the Lakers report directly using a team shortcode or Discord custom emoji; the team argument is optional |
| `!nba games` | Scoreboard PNG with team logos, live scores/clocks, and upcoming tip-offs; finished, postponed, canceled, and suspended games are excluded |
| `!nba boxscores` | All games on the current NBA slate, with a dropdown to open any game's full box score |
| `!nba boxscore` | Alias for `!nba boxscores`; dates work with either spelling |
| `!nba picks Lakers` | 2027-2030 draft ownership, both rounds, protections and swaps |
| `!nba pick :lakers:` | Same report; typed/custom team emojis, names and ESPN abbreviations work |
| `!nba boxscores 2026-10-26` | Game selector for a specific NBA date; short dates such as `26/Oct/2026` also work |
| `!nba Paul George` | Current NBA slate, or latest played game if his team has no game on that slate |
| `!nba Paul George 2025-10-22` | Game on a specific NBA date |
| `!nba Jalen Duren 26/Dec` | Game on the most recent 26 December |
| `!nba Jalen Duren 26/Dec/2025` | Game on 26 December 2025 |
| `!watch Paul George` | One message refreshed every 30 seconds until final |
| `!unwatch` | Stop your watch in this channel |
| `!season Paul George` | Current regular-season per-game averages |
| `!season LeBron James 2025-26` | Selected regular season |
| `!season LeBron James 2026 playoffs` | Playoffs, using the season-ending year |
| `!nbahelp` | Help |

Player matching accepts full names, unique partial names, accent-insensitive names, and ESPN athlete IDs. Ambiguous names require a more specific query. The directory includes current rosters. Dated `!nba` lookups check the full slate, including appearances for a former team.

Misspellings such as `!nba Ty Jermone` offer a **Did you mean Ty Jerome?** confirmation. Choose a suggested player or **No thanks**; only the requester can respond, and the buttons expire after 60 seconds. Suggestions never automatically select a player. The same confirmation works with `!watch`, `!season`, and dated lookups, keeping the original command options after confirmation.

Dates accept `YYYY-MM-DD`, `26/Dec`, and `26/Dec/2025` (month abbreviations ignore case). Without a year, the date resolves to its most recent occurrence using the NBA's New York calendar date. The resolved full date appears on the card or suggestion. Invalid dates are rejected.

If the player has no recorded appearance on the requested date (including DNP), the bot searches their available season game logs for the closest recorded appearance. Equal distances prefer the earlier game. It asks whether to show that box score with **Yes** / **No thanks** buttons. Only the requester can choose; the buttons expire after 60 seconds. No alternative card loads until Yes is clicked. All-Star exhibitions are excluded from suggestions. Missing provider data is reported as a feed error rather than treated as a DNP. Without a date, the bot uses the current slate; if the team has no game there, it shows the latest recorded appearance (including before a trade), labelled with the actual game date and “Latest played game”. Future appearances are excluded from this fallback.

One watch per user per channel, with 20 total by default. Starting another replaces the previous watch. Watches stay attached to their original game across midnight, end after final/cancellation or eight hours, and clear on restart. A failed refresh marks the existing message as delayed; five failures stop it. `REFRESH_SECONDS` has a minimum of 30. Large numbers of watched cards can take longer than one refresh interval because image rendering is queued.

The games scoreboard separates **Live now** and **Coming up** into matchup tiles. Tip-off times use **Europe/Dublin** by default and show the GMT/IST label. Set `GAMES_TIME_ZONE` to another IANA time zone if needed. Scheduled games past their tip-off time show **Awaiting tip-off** until the feed reports them live; delayed games are labelled explicitly. The image stays sharp when opened or zoomed.

## Player card pages

The injury report includes every player in ESPN's current injury feed, including day-to-day entries. Choose any of the 30 teams (even teams with no entries), or All NBA teams. Each PNG shows injury/reason, reported status, a short source note, and expected return. Return dates are labelled ESPN estimates; explicit missing timetables show TBD, season-ending entries show Out for season, and past estimates show Update pending. Dates are never inferred from the date an injury was reported. Use Previous/Next for all pages and Refresh for newer data (feed cache: one minute). Team selection refreshes the report; pagination keeps a consistent snapshot. Menus expire after one hour or restart. `!nba injuries` is an alias.

`!nba boxscores` includes live, upcoming, and finished games. Choose a matchup from the dropdown to replace the scoreboard with a sharp PNG showing both teams' full rosters, starters, bench players, DNP reasons, shooting, counting stats, and provider-supplied team totals. Use **Previous Day** and **Next Day** to browse NBA dates. **Refresh** requests the latest score, game clock and box score while keeping your selected game open; on the overview it refreshes all matchups. Refresh bypasses the bot data cache. Choose a different game or use **All Games** to return to that date's slate. Both `!nba boxscore` and `!nba boxscores` support optional dates. Pregame selections explain that stats are pending. Menus expire after one hour or a restart. These are snapshots, not automatic watches.

Game cards have three full-resolution PNG pages: **Statline** (the landing page), **Quarter by Quarter**, and **Shot Breakdown**. The landing page has separate buttons for the two breakdowns. Each detail page has a **Statline** button and a direct button for the other detail page. All three pages stay sharp when opened or zoomed; there is no GIF playback. The detail pages are prepared in the background after the landing page appears.

The player directory and Chromium warm up at startup. Rendering reuses a browser page, waits for image decoding rather than network idle, and coalesces/caches identical render requests. Updated stats invalidate cached faces; live refreshes never keep a stale face just to save rendering time.

Anyone viewing the card can select its page. Live watches refresh the selected page. Buttons expire after eight hours or a bot restart; request a new card to continue. Season-average cards do not have game-breakdown buttons.

The quarter page shows PTS, REB, AST, STL, BLK, TO, FG, 3FG, and FT for Q1–Q4 and each overtime. Minutes and plus/minus by quarter are not supplied by this feed and are not estimated. The shot page shows makes/attempts, shooting percentage, and points for dunks, layups, floaters, threes, short mid-range (10–15 ft), long mid-range (16+ ft for two-pointers), other paint shots (<10 ft), hooks, tip-ins, other two-pointers, and free throws. Categories do not overlap. Missing distances stay in Other 2-pointers; three-point attempts remain threes regardless of distance.

Counts come from ESPN play-by-play and are checked against the box score. A discrepancy is labelled **Partial feed**; missing play-by-play and quarters not started show `—`. The game card still works when play-by-play is unavailable.

Missed heaves listed in the play feed are removed from individual shooting counts only when both official FG and 3FG totals confirm they were team-only attempts. Historical games that charge the player retain them.

## Data and scoring

Public ESPN feeds supply the directory, scoreboard, box score, and season game logs. No sports API key is required. These are public, undocumented endpoints, so availability and schema may change. Refreshing every 30 seconds does not guarantee the provider has updated that quickly. No Real App rating, reactions, or social data is copied.

Season averages are calculated from the selected season's recorded appearances, separated into regular season/playoffs. All-Star games, the NBA Cup championship, and play-in games are excluded. They are cached for one minute and may lag the live game card. Shooting percentages use total makes/attempts, rather than averages of game percentages. Minutes are based on the provider's rounded game-log minutes. Missing historical fields such as plus/minus and offensive/defensive rebounds remain `—`.

Fantasy points use [ESPN's default points-league scoring](https://www.espn.com/fantasy/basketball/story/_/id/30296896/espn-fantasy-default-points-league-scoring-explained), calculated from ESPN box-score stats: **PTS + 2×FGM − FGA + FTM − FTA + 3PM + REB + 2×AST + 4×STL + 4×BLK − 2×TO**. Season FP averages use the same formula on season totals, divided by appearances. These are calculated values, not scores fetched from a specific fantasy league; custom ESPN league scoring may differ. Missing required stats show `—`. TS% is **100×PTS / [2×(FGA + 0.44×FTA)]**.

## Verification and preview

```powershell
npm.cmd test
npm.cmd run preview -- --demo
npm.cmd run preview -- nba Anthony Edwards --back
npm.cmd run preview -- nba Tyrese Maxey 2025-10-22
npm.cmd run preview -- season LeBron James 2025-26
```

Previews save `preview.png` in this folder and do not connect to Discord. The demo is clearly marked as sample data. Puppeteer downloads Chrome during normal `npm.cmd ci`; if unavailable, set `PUPPETEER_EXECUTABLE_PATH` to a local Chrome installation. In PowerShell, use `$env:PUPPETEER_EXECUTABLE_PATH='C:/Program Files/Google/Chrome/Application/chrome.exe'`. For a container that requires disabling Chrome's sandbox, explicitly set `PUPPETEER_NO_SANDBOX=true`. The `.cmd` suffix avoids PowerShell execution-policy restrictions on npm's script wrapper.

## Draft picks

`!nba picks <team>` and `!nba pick <team>` show 2027, 2028, 2029 and 2030
inclusive, on one team-branded PNG. Each year has first- and second-round
ownership, outgoing obligations, pick protections, and conditional swap rights.
No projected pick positions or unconditional totals are inferred from those rights.
Names, ESPN abbreviations and typed/custom team emojis are supported.

The source is a RealGM ownership snapshot verified on **29 October 2026**,
packaged in `future_picks.json`. The source blocks direct requests from Fly, so
this is **not a live trade feed**. The report prominently shows its verification
date and notes that later trades may change ownership. Source buttons open the
ownership table and detailed trade conditions. It does not silently substitute
Tankathon's single-draft projection for missing future years.

When updating the snapshot, recheck all 30 teams and both rounds for every year,
retain all conditional clauses, update `verifiedOn`, and redeploy. A missing or
duplicate year or an invalid verification date produces an error rather than
invented retained picks. Tests cover all 120 team/year records.

## Fly deployment

Fly app: `nba-live-bot`, region `ewr`. `fly.toml` defines one always-on 1 GB worker with no public HTTP service. The Docker image includes Chromium and runs the tests and a demo card render during its build. `.dockerignore` excludes `.env` and other local assets; credentials are stored in Fly secrets.

Deploy changes from this folder:

```powershell
fly deploy --app nba-live-bot --remote-only --ha=false --yes
fly logs --app nba-live-bot
fly status --app nba-live-bot
```

Keep only one bot instance running. Stop your local `npm.cmd start` process with Ctrl+C when using the Fly deployment, to prevent duplicate replies. Watches are held in memory and clear on redeployment or restart. This bot does not require a persistent volume.
