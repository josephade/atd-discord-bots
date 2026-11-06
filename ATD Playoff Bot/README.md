# ATD Sim Bot: simple playoff workflow

## Two commands

In the private admin channel (1533242104304631969), run:

```text
!simcreate ATD 113
```

This reads the final voting standings, selects the top 16 seeds, resolves their
server team logos, imports all 160 roster players/seasons from the Team Sheet,
loads available exact-season NBA baselines, and builds the bracket. It opens
the series control panel automatically. You do not need separate simteam,
simimport, simloadstats, simbracket, focus, odds or penalty commands.

To reopen the panel after timeout/restart:

```text
!sim
```

`!simsetup ATD 113` is also supported as an alias for automatic setup.
Repeating setup for an existing bracket opens its controls without replacing
seeds, voting odds or saved results. It cannot overwrite a different active ATD.
Previously entered manual teams without a bracket are replaced atomically by
the verified complete sheet import; there is no partially created bracket if
one team or source is invalid. Long imports show per-team progress. Missing
player-season baselines are labelled as estimated and listed in an attached
report; source failures never substitute different seasons or player identities.

## A new ATD voting sheet

ATD 113's voting and roster sources are registered. For ATD 114, give the new
voting sheet URL in the same setup command:

```text
!simcreate ATD 114 | https://docs.google.com/spreadsheets/d/YOUR_ID/edit#gid=YOUR_TAB
```

The source is remembered for future use. The default roster workbook is the
existing Team Sheet. Its ATD tab is detected by number; if there are several
matching tabs, specify the exact roster tab in the same command:

```text
!simcreate ATD 114 | <voting URL> | ATD 114 - Anon Budget
```

The voting tab must contain one completed Team / Wins / Losses / GM table.
Exact decimal wins determine seeds; sheet order breaks exact ties, matching
the Voting Bot. No GM names/IDs are copied into simulation reports.

## Use the panel

1. Pick a matchup from the series dropdown.
2. Open **Series Settings** and enter the higher seed's official base win
   percentage plus each team's suspect deduction (0 or 10 percentage points).
   Settings are restricted to commissioners in the private admin channel.
3. **Review Live Lineups** shows the current starters and bench from Google.
4. **Play Game 1** checks the sheet again immediately before generating a game.
5. Use **Play Next Game** below the resulting scorecard. Existing box-score and
   series-average buttons remain. Double-clicking a bound button returns that
   same game's saved result and cannot accidentally play an extra game.
6. Choose other matchups from the panel. Once every series finishes, **Next
   Round** creates the next matchups. Repeat through the Finals.

The panel also has Bracket, Series Averages and Refresh buttons. On game cards,
**Series Controls** reopens the panel. Buttons are commissioner-only for actions;
GM box-score/average buttons remain available. Views expire after 30 minutes
(15 minutes on game cards); !sim or !boxscore reopens fresh controls.

## Live lineup policy

Every *new game*, in every round, fetches both teams from the active Google
roster tab. A GM can move any of their ten drafted players between starting
slots and bench slots before the next game. The bot follows the sheet order;
first five rows are PG/SG/SF/PF/C, then Bench 1-5. Simulations continue to use the
existing odds model: lineup changes influence generated player minutes/stats,
not the already commissioned win probabilities.

Lineup order and player years are live; the eligible set of ten player names is
fixed for the series. An incomplete sheet, changed player identity, unreadable
source or conflicting series change blocks a roll rather than using stale
lineups. Existing profiles travel with a matching name/year. Changed years use
an exact cached NBA baseline if available, otherwise an explicitly labelled
estimate; game simulation does not wait for fresh NBA API requests.

Each game stores its exact lineup/profile snapshot, read timestamp, random seed
and result. Old scorecards and audits use their saved snapshot. Series averages
follow player identity across slot changes, never row position. Retrieving a
saved game does not read the current sheet and never rerolls it.

## Basketball rules and data

Higher seed hosts Games 1, 2, 5 and 7. HCA adds 10 percentage points. A 47% home
team becomes 57%; an applicable 10-point suspect deduction makes it 47% again.
Probabilities clamp at 0-100%. Official voting odds determine the winner; score,
quarter totals and player performances are synthetic and consistent with it.

Suspect deductions are entered in Series Settings before Game 1. They apply to
that entire next series, do not stack, and do not automatically transfer from
Theme Bot's ledger. New rounds begin with fresh matchup odds and zero
unentered deductions. These controls never modify the real voting/roster sheets.

## GM commands

- !playoffs: bracket and champion (also !playoff and !playofffs).
- !series [ID]: odds, matchup and results.
- !boxscore <game> [ID] / !boxscores: saved game with detail buttons.
- !seriesstats [ID]: saved simulated averages.
- !simroster <seed>: live Google Sheets lineup.
- !simvote <ID> <0-7>: optional built-in ballot; DM supported.
- !simhelp: short workflow; !simhelp advanced: optional manual controls.

## Hosting and advanced controls

This is the separate ATD Sim Bot Discord application on Fly app
atd-playoff-bot, one machine with persistent /data SQLite. Current environment
and Discord credentials remain unchanged. Owner/commissioner access and
configured channel/voter restrictions are enforced for buttons as well as
commands. Setup never invents voting odds or plays a game automatically.

Docker runs the complete offline test suite before deployment. Read-only
Google credentials and the new bot token are Fly secrets, excluded from the
image. See [ADVANCED.md](ADVANCED.md) for local/Fly installation, optional
manual setup, voting formula, exports, archive and test-season commands.


## Channels, GM simulation and cancellation

Public commands and button actions are limited to #atd-sims
(934054991965077564) and its threads; private admin channel
1533242104304631969 remains available. The bot joins accessible existing/new
public sim threads. Add it explicitly to private threads. Run !sim and select
a series inside each match thread once; thread selections are independent.

Commissioners set odds first and use **Team GMs** privately to assign verified
Discord IDs/mentions. Confirmed historical ATD 113 team mappings are supplied
through a private Fly secret, never printed on public cards. Both co-GMs may
then use !sim Game 1 for their own series. Unmapped teams require commissioner
assignment. GMs cannot change odds or simulate other series.

!simcancel ATD <number> in the private admin channel stops the exact named
active ATD or pending import, clears old focus bindings, preserves saved games,
and allows a new ATD. A stale cancelled setup cannot reinstall itself, and old
panels cannot advance a new tournament. Normal archive rules remain separate.

Copy-ready Discord guides: [Commissioners](guides/commissioner-discord.md) and
[GMs](guides/gm-discord.md).

Backup private admin channel: 939947875805302784. It supports the same commissioner commands and settings/GM-assignment controls as 1533242104304631969, with the same role and private-channel checks.
