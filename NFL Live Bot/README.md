# NFL Live Bot

A standalone Discord bot for real NFL games. It creates a dark PNG stat card with a player headshot, team, score, game clock, and the stats for that player's position (passing, rushing, receiving, defense, or kicking), plus fantasy points.

## Setup

Use Node.js 22 or later. In PowerShell:

```powershell
cd 'NFL Live Bot'
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
| `!nfl injury` | All listed NFL injuries, paginated, with team selectors and a Refresh button |
| `!nfl injury Eagles` | Open a team's report directly; full names, unique partial names, abbreviations and team IDs work |
| `!nfl injury :eagles:` | Open the Eagles report directly using a team shortcode or Discord custom emoji; the team argument is optional |
| `!nfl games` | Scoreboard PNG with team logos, live scores/clocks, and upcoming kickoffs; finished, postponed, canceled, and suspended games are excluded |
| `!nfl boxscores` | All games on the current NFL slate, with a dropdown to open any game's full box score |
| `!nfl boxscore` | Alias for `!nfl boxscores`; dates work with either spelling |
| `!nfl boxscores 2026-10-26` | Game selector for the NFL week containing that date; short dates such as `26/Oct/2026` also work |
| `!nfl Patrick Mahomes` | Current NFL slate, or latest played game if his team has no game on that slate |
| `!nfl Patrick Mahomes 2025-10-22` | Game on a specific NFL date |
| `!nfl Jalen Hurts 26/Dec` | Game on the most recent 26 December |
| `!nfl Jalen Hurts 26/Dec/2025` | Game on 26 December 2025 |
| `!watch Patrick Mahomes` | One message refreshed every 30 seconds until final |
| `!unwatch` | Stop your watch in this channel |
| `!season Patrick Mahomes` | Current regular-season per-game averages |
| `!season Jalen Hurts 2025-26` | Selected regular season |
| `!season Jalen Hurts 2026 playoffs` | Playoffs, using the season's starting year |
| `!nflhelp` | Help |

Player matching accepts full names, unique partial names, accent-insensitive names, and ESPN athlete IDs. Ambiguous names require a more specific query. The directory includes current rosters. Dated `!nfl` lookups check the full slate, including appearances for a former team.

Misspellings such as `!nfl Patrik Mahomes` offer a **Did you mean Patrick Mahomes?** confirmation. Choose a suggested player or **No thanks**; only the requester can respond, and the buttons expire after 60 seconds. Suggestions never automatically select a player. The same confirmation works with `!watch`, `!season`, and dated lookups, keeping the original command options after confirmation.

Dates accept `YYYY-MM-DD`, `26/Dec`, and `26/Dec/2025` (month abbreviations ignore case). Without a year, the date resolves to its most recent occurrence using the league's New York calendar date. The resolved full date appears on the card or suggestion. Invalid dates are rejected.

If the player has no recorded appearance on the requested date (including inactive/DNP), the bot searches their available season game logs for the closest recorded appearance. Equal distances prefer the earlier game. It asks whether to show that box score with **Yes** / **No thanks** buttons. Only the requester can choose; the buttons expire after 60 seconds. No alternative card loads until Yes is clicked. Pro Bowl appearances are excluded from suggestions. Missing provider data is reported as a feed error rather than treated as a DNP. Without a date, the bot uses the current slate; if the team has no game there, it shows the latest recorded appearance (including before a trade), labelled with the actual game date and "Latest played game". Future appearances are excluded from this fallback.

One watch per user per channel, with 20 total by default. Starting another replaces the previous watch. Watches stay attached to their original game and end after final/cancellation or eight hours, and clear on restart. A failed refresh marks the existing message as delayed; five failures stop it. `REFRESH_SECONDS` has a minimum of 30. Large numbers of watched cards can take longer than one refresh interval because image rendering is queued.

The games scoreboard separates **Live now** and **Coming up** into matchup tiles. Kickoff times use **Europe/Dublin** by default and show the GMT/IST label. Set `GAMES_TIME_ZONE` to another IANA time zone if needed. Scheduled games past their kickoff time show **Awaiting kickoff** until the feed reports them live; delayed games are labelled explicitly. The image stays sharp when opened or zoomed.

## Player card pages

The injury report includes every player in ESPN's current injury feed, including questionable/doubtful entries. Choose any of the 32 teams (even teams with no entries), or All NFL teams. Each PNG shows injury/reason, reported status, a short source note, and expected return. Return dates are labelled ESPN estimates; explicit missing timetables show TBD, and past estimates show Update pending. Dates are never inferred from the date an injury was reported. Use Previous/Next for all pages and Refresh for newer data (feed cache: one minute). Team selection refreshes the report; pagination keeps a consistent snapshot. Menus expire after one hour or restart. `!nfl injuries` is an alias.

`!nfl boxscores` includes live, upcoming, and finished games for the current NFL week — the league plays Thursday/Sunday/Monday (plus the occasional Saturday late season), not nightly, so browsing moves by week rather than by calendar day. Choose a matchup to open **Game Overview** first: score and result, quarter-by-quarter scoring, passing/rushing/receiving/tackle leaders from both teams, and team comparisons. Then use the stats dropdown to switch between each team's Offense, Defense, and Special Teams. Large tables split into numbered sections, keeping every image close to the NBA box-score size with larger text. Every player, category, and provider total is retained across the pages. Stats remain grouped into Passing, Rushing, Receiving, Defensive, Kicking, and other provider categories (not one shared table — the categories a team shows depend on who recorded a stat that game). Use **Previous Week** and **Next Week** to browse adjacent NFL weeks, including across the preseason/regular season/postseason boundary; they disable at the start of the preseason and after the Super Bowl. **Refresh** requests the latest score, game clock and box score while keeping your selected game and stats section open; on the overview it refreshes all matchups. Refresh bypasses the bot data cache. Choose a different game or use **All Games** to return to that week's slate. Both `!nfl boxscore` and `!nfl boxscores` support an optional date, which jumps to the NFL week containing it. Pregame selections explain that stats are pending. Menus expire after one hour or a restart. These are snapshots, not automatic watches.

Game cards have three full-resolution PNG pages: **Statline** (the landing page), **Quarter by Quarter**, and **Drive Summary**. The landing page has separate buttons for the two detail pages. Each detail page has a **Statline** button and a direct button for the other detail page. All three pages stay sharp when opened or zoomed; there is no GIF playback. The detail pages are prepared in the background after the landing page appears.

The player directory and Chromium warm up at startup. Rendering reuses a browser page, waits for image decoding rather than network idle, and coalesces/caches identical render requests. Updated stats invalidate cached faces; live refreshes never keep a stale face just to save rendering time.

Anyone viewing the card can select its page. Live watches refresh the selected page. Buttons expire after eight hours or a bot restart; request a new card to continue. Season-average cards do not have game-detail buttons.

The quarter page shows each team's score per quarter (and overtime), from ESPN's official scoring summary — not an inferred per-player play count, since ESPN's NFL feed does not attribute individual plays to specific players. The viewed player's team is highlighted.

The drive summary page lists every drive of the game — quarter, team, starting field position, number of plays, time of possession, yards gained, and result (touchdown, field goal, punt, turnover, etc.) — again at the team level. ESPN's NFL play-by-play has no structured per-athlete participant list (only team-level), so this bot does not attempt to fabricate a per-player play log the way a basketball shot chart can be built from play text; it shows exactly what the provider verifiably supports. The viewed player's drives are highlighted. Unavailable before kickoff.

## Data and scoring

Public ESPN feeds supply the directory, scoreboard, box score, and season game logs. No sports API key is required. These are public, undocumented endpoints, so availability and schema may change. Refreshing every 30 seconds does not guarantee the provider has updated that quickly. No Real App rating, reactions, or social data is copied.

Season averages are calculated from the selected season's recorded appearances, separated into regular season/playoffs. Pro Bowl appearances are excluded. They are cached for one minute and may lag the live game card. Rate stats (completion %, yards/attempt, field-goal %, etc.) are recomputed from the season's summed counting stats, not averaged game-to-game. "Longest" fields show the season's single best play, not a sum or average. Missing historical fields remain `—`.

Fantasy points use **ESPN standard PPR scoring**: 0.04 pts/passing yard, 4/passing TD, −2/interception thrown, 1 pt/reception, 0.1 pts/rushing or receiving yard, 6/rushing or receiving TD, −2/fumble lost. Kickers score 3 pts/field goal under 40 yards, 4 for 40–49, 5 for 50+, −1/missed field goal, and 1/extra point. The single-game card reads made-kick distances from the game's scoring plays (falling back to a flat 3/FG if they can't be matched); `!season` uses the gamelog's distance-bucketed fields. **Defense-only players (tackles/sacks/interceptions, no offensive stat line) show no fantasy-points tile** — there is no single universally "default" individual-defensive-player scoring convention to cite honestly, unlike the formula above. These are calculated values, not scores fetched from a specific fantasy league; custom league scoring may differ. Missing required stats show `—`. Other efficiency figures (passer rating, QBR, yards/attempt, field-goal %) are ESPN's own provider-computed values, shown as reported rather than re-derived.

## Verification and preview

```powershell
npm.cmd test
npm.cmd run preview -- --demo
npm.cmd run preview -- nfl Justin Jefferson --back
npm.cmd run preview -- nfl Justin Jefferson 2025-10-22
npm.cmd run preview -- season Patrick Mahomes 2025-26
```

Previews save `preview.png` in this folder and do not connect to Discord. The demo is clearly marked as sample data. Puppeteer downloads Chrome during normal `npm.cmd ci`; if unavailable, set `PUPPETEER_EXECUTABLE_PATH` to a local Chrome installation. In PowerShell, use `$env:PUPPETEER_EXECUTABLE_PATH='C:/Program Files/Google/Chrome/Application/chrome.exe'`. For a container that requires disabling Chrome's sandbox, explicitly set `PUPPETEER_NO_SANDBOX=true`. The `.cmd` suffix avoids PowerShell execution-policy restrictions on npm's script wrapper.

## Fly deployment

Fly app: `nfl-live-bot`, region `ewr`. `fly.toml` defines one always-on 1 GB worker with no public HTTP service. The Docker image includes Chromium and runs the tests and a demo card render during its build. `.dockerignore` excludes `.env` and other local assets; credentials are stored in Fly secrets.

Deploy changes from this folder:

```powershell
fly deploy --app nfl-live-bot --remote-only --ha=false --yes
fly logs --app nfl-live-bot
fly status --app nfl-live-bot
```

Keep only one bot instance running. Stop your local `npm.cmd start` process with Ctrl+C when using the Fly deployment, to prevent duplicate replies. Watches are held in memory and clear on redeployment or restart. This bot does not require a persistent volume.
