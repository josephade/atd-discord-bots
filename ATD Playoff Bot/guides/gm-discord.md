**ATD SIM BOT ? GM GUIDE**

Use <#934054991965077564> or your matchup's thread inside it. A commissioner sets the odds, registers team GMs privately and links the matchup to its thread first.

**Play your matchup**
`!sim Game 1`
`!sim Game 2`
Continue with the next number until either team reaches **4 wins**. Only registered GMs of those teams (including co-GMs), or a commissioner, can simulate the series. You can also use **Play Next Game** below a scorecard. Repeating a game returns its saved result; it cannot reroll it. You cannot change odds, suspect deductions or another team's series.

**All GM commands**
`!sim` ? your series status.
`!sim Game <1-7> [series ID]` ? simulate your own matchup's next game; the ID is unnecessary in its linked thread.
`!playoffs` ? bracket/series scores/champion. Aliases: `!playoff`, `!playofffs`.
`!series [series ID]` ? matchup odds, HCA and results.
`!boxscore <game> [series ID]` ? saved scorecard; buttons open each team's box score and series averages. Alias: `!boxscores`.
`!seriesstats [series ID]` ? averages from saved simulated games.
`!simroster <seed>` ? current Google Sheets lineup, without GM identities.
`!simvote <series ID> <0-7>` ? optional in-bot prediction ballot; use only if the commissioner is collecting votes here. DMs supported. Latest ballot replaces the earlier one until voting closes.
`!simhelp` ? command help.

**Optional voting choices**
0/1/2/3 = higher seed wins in 4/5/6/7 games.
4/5/6/7 = lower seed wins in 7/6/5/4 games.

Your starting five and bench order refresh from Google Sheets before every new game. Already-played box scores stay unchanged. HCA is +10 percentage points in Games 1, 2, 5 and 7 for the higher seed. Displayed stats are simulated; player data gaps are labelled as estimates. Ask a commissioner if your team is not linked or you need the bot added to a private thread.
