**ATD SIM BOT ? COMMISSIONER GUIDE**

**Where to use it**
? Public games/results: <#934054991965077564> and threads inside that channel.
? Setup, odds, private GM assignments and cancellation: <#1533242104304631969> or backup <#939947875805302784>.
? The owner, Administrator/Manage Server users and the configured commissioner role can administer it.

**Normal workflow**
1. In the private channel: `!simcreate ATD 113`.
   For a new voting source: `!simcreate ATD 114 | <voting-sheet URL>`.
   Seeds, logos, rosters, available player baselines and bracket import together.
2. Use the dropdown to choose a series. **Series Settings** handles base odds and suspect deductions together.
3. **Team GMs** privately assigns each team's Discord user IDs/mentions, including both partners. Confirmed ATD 113 assignments are already available. New/unmapped teams need this step before their GMs can roll. Clearing a team's field revokes GM simulation access. No GM names are printed on game cards.
4. Create/open that matchup's thread in #atd-sims. Run `!sim` in the thread, then select its matchup from the dropdown. This links only that thread; other threads keep their own series.
5. Its registered GMs can now use `!sim Game 1`, `!sim Game 2`, etc., or Play Next Game. Commissioners can also play games. Stop at four wins. Odds and suspect deductions lock after Game 1; Google lineup order refreshes before every new game.
6. Use the private panel's **Next Round** after every series in the round finishes. Set new matchup odds and link the next match threads.

**Common commands**
`!simcreate ATD <number> [| voting URL [| roster tab]]` ? automatic setup / reopen.
`!simsetup ATD <number> [| voting URL [| roster tab]]` ? same automatic setup.
`!sim` ? open controls in this channel/thread.
`!sim Game <1-7> [series ID]` ? play the next numbered game or return its saved result.
`!simcancel ATD <number>` ? PRIVATE: stop the named active tournament (or pending setup), preserve games and allow a new ATD. Wrong ATD numbers are rejected. Old controls cannot affect the new tournament.
`!simarchive` ? PRIVATE: archive finished playoffs; labelled tests may end at any point.
`!simexport` ? PRIVATE: export results, ballots and audit data.
`!simaudit <series ID> <game>` ? PRIVATE: inspect/reproduce a saved roll.
`!simhelp` ? short guide; `!simhelp advanced` ? optional manual commands.

**Optional manual controls** (private except focus)
`!simteam <seed> <emoji> <team name>` ? register/update a team before bracket creation.
`!simsheet <URL> | <tab>` ? configure the live roster sheet.
`!simimport <seed>` ? import that team's ten roster slots.
`!simsetroster <seed>` followed by ten `Player | Season` lines ? manual roster.
`!simloadstats <seed>` ? try exact-season NBA baselines before the bracket.
`!simprofile <seed> <slot 1-10> <PPG> <RPG> <APG>` ? manual baseline before the bracket.
`!simbracket` ? create/open bracket controls.
`!simresetbracket CONFIRM` ? reset an entirely unplayed bracket and its ballots; never deletes played games.
`!simodds <series ID> <higher-seed percent>` ? enter official base odds.
`!simpenalty <series ID> <seed> [10 or 0]` ? set/remove a suspect deduction before Game 1.
`!simclosevotes <series ID>` ? derive odds from optional built-in ballots.
`!simfocus <series ID>` ? bind the current public channel/thread (or private channel) to a series.
`!simadvance` ? create the next round after all current series finish.
`!simtest <name>` ? create an explicitly labelled test season; then use automatic setup or manual controls.

Commissioners can also use every viewing/voting command in the GM guide. `series ID` is the ID shown by the bot, not an ATD number or seed. For private threads, add the bot explicitly. Cards/box scores are simulated basketball stats, while commissioned voting odds decide the winner. Saved results cannot be rerolled.
