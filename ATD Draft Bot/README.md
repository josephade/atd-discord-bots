# ATD Draft Bot

A Discord bot for running snake drafts with AI-controlled teams. The bot manages the full draft lifecycle — player pool ingestion, pick order, AI team evaluation, result output, and a post-draft feedback system for iterative weight tuning.

---

## Architecture

```
ATD Draft Bot/
├── bot.py               # Discord command handlers and draft orchestration
├── ai_drafter.py        # AI pick logic — effective ADP scoring engine
├── draft_manager.py     # Draft state machine, team slots, snake order
├── player_data.py       # Player metadata: tiers, positions, archetypes, pool categories
├── player_positions.py  # Position slot definitions
├── weights.json         # Tunable penalty/bonus values loaded at runtime
├── config.py            # Environment variable bindings
├── requirements.txt
└── feedback/
    ├── db.py            # SQLite persistence (drafts, reviews, proposals, weight history)
    ├── analyzer.py      # Signal computation from review data
    └── proposer.py      # Proposal generation, formatting, and application
```

---

## Requirements

- Python 3.10+
- A Discord bot token with `Message Content Intent` enabled
- A Google service account with access to the player pool spreadsheet
- Dependencies: `discord.py>=2.3.0`, `gspread>=5.10.0`, `oauth2client>=4.1.3`, `python-dotenv>=1.0.0`

Install dependencies:

```bash
pip install -r requirements.txt
```

---

## Configuration

Create a `.env` file in this directory with the following variables:

```env
DISCORD_TOKEN=your_discord_bot_token
DRAFT_CHANNEL_ID=the_channel_id_where_drafts_run

POOL_SPREADSHEET_ID=google_sheet_id_containing_the_player_pool
POOL_TAB_NAME=East

OUTPUT_SPREADSHEET_ID=google_sheet_id_where_results_are_written
```

Place your Google service account credentials in `service_account.json` in this directory.

---

## Running the Bot

```bash
python bot.py
```

---

## Draft Modes

The bot supports three draft modes, selectable at the start of each `!draft` session:

- **Standard** — one Discord user per team, remaining teams are AI-controlled.
- **Multi-team** — a single user controls multiple teams simultaneously and competes against AI teams.
- **Watch** — fully automated draft with no human input.

In all modes, the user can choose between a random lottery position or manually selecting their draft slot.

---

## AI Evaluation System

Each available player is scored with an **effective ADP** — a modified version of their raw average draft position. Lower score means higher priority. The AI picks the lowest-scoring available player each round, with a small random jitter applied for variety.

The score is calculated by applying a layered set of adjustments on top of raw ADP:

1. **Hard stops** — do-not-draft list (+300), unknown players (+150), full position slots (+500).
2. **Tier diversity** — bonus for filling a tier not yet represented on the team.
3. **Bench-only penalty** — large penalty for bench-only picks while starter slots remain open.
4. **Scorer distribution** — pull toward shot creators in rounds 1-5; bench scorer pull in rounds 6-10.
5. **Chemistry conflicts** — penalties for ball-dominant stacking, non-scoring big redundancy, and soft big duos.
6. **Elite starter redundancy** — penalises drafting a high-ADP player behind an already-elite starter.
7. **Position priority** — Center slot is highest priority when empty; SF second.
8. **Star-specific complements** — PnR creators pull toward PnR bigs; elite distributors pull toward scoring wings.
9. **Defensive compensation** — when perimeter defense is weak across the starting three, the AI targets defensive frontcourt players and bench perimeter defenders.
10. **Spacing and shooting** — pull toward shooters when spacing is deficient; non-shooters penalised when the team already lacks spacing.
11. **Backup center** — dedicated pull for a backup C in rounds 6-9.
12. **Fall protection floor** — no player can fall more than N picks past their raw ADP per round (2 for Tier 1-2 in R2, 7 in rounds 3-5, 15 in bench rounds).
13. **Global overdue override** — forces a team to take a player who has been passed over beyond their tier deadline.

All penalty and bonus magnitudes are stored in `weights.json` and can be adjusted through the RLHF feedback system without restarting the bot.

---

## RLHF Weight Tuning System

After each draft completes, the bot saves all team rosters to a local SQLite database (`draft_feedback.db`). The feedback loop works as follows:

1. Run `!draftreview` to start a review session. The bot presents each team's roster with Approve / Reject buttons.
2. On rejection, select one or more reasons from a button menu (ball-dominant conflict, no scorer, no defense, etc.).
3. After all teams are reviewed, the analyzer aggregates rejection patterns and computes nudge signals per weight key.
4. Proposed weight changes are posted in Discord. Users with the `ATD Bot Developer` or `ATD Bot Tester` role can confirm, skip, or override individual proposals.
5. On confirmation, `weights.json` is updated and the AI reloads weights immediately without a restart.

The minimum rejection threshold before a change is proposed is 3 teams citing the same reason. Maximum nudge per cycle is 30% of current value, clamped to per-key bounds defined in `analyzer.py`.

---

## Commands

### Draft

| Command | Description |
|---|---|
| `!draft` | Start a new draft session |
| `!draftcancel` | Cancel the active draft in this channel |
| `!draftskip` | Sim all remaining picks instantly using AI |
| `!draftstatus` | Show live pick number, round, current team, and progress |
| `!drafthistory` | Show the last 10 completed drafts with timestamps and review status |
| `!draftboard` | Show all team rosters |
| `!draftboard <emoji>` | Show one team's roster |
| `!draftpool` | Browse available players (paginated, with navigation buttons) |
| `!draftpool <category>` | Filter by position group: `Guard`, `Wing`, `Forward`, `Big` |
| `!drafthelp` | Full command reference |

### Review and Weight Tuning

| Command | Description |
|---|---|
| `!draftreview` | Start or resume a review session for the most recent draft |
| `!confirmweights` | Apply all proposed weight changes |
| `!skipweights 1 3` | Apply all proposals except the specified ones |
| `!setweight 2 55` | Override a specific proposal value before confirming |
| `!cancelweights` | Discard all pending proposals |
| `!currentweights` | Display all current weight values |
| `!weighthistory` | Show the last 20 weight changes |

Weight-modifying commands require the `ATD Bot Developer` or `ATD Bot Tester` Discord role. Review commands are open to all users.

---

## Pick Format (Human Players)

When it is a human player's turn, they have 120 seconds to submit a pick in the following format:

```
<pick_number>. <team_emoji> Player Name
```

Example:

```
14. [emoji] LeBron James
```

The bot uses fuzzy name matching, so minor typos are tolerated. If the submitted name matches a player already drafted, the bot will reject it and prompt again.

---

## Notes

- Multiple drafts can run simultaneously in separate threads off the main draft channel.
- Review sessions persist across bot restarts — `!draftreview` resumes from the last unreviewed team.
- The `started_by` field is recorded per draft and visible in `!drafthistory`.
- Draft position can be assigned randomly (lottery) or chosen manually at setup time.
