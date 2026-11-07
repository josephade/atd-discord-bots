# TFM ADP Player Pricing

A voting site where ATD drafters say what each of 313 players should cost, from Michael Jordan through Ben Simmons. The site only collects votes. It doesn't change the live ADP sheet or anything the Discord bots read.

## Run it locally

```
pip install -r requirements.txt
copy .env.example .env
python server.py
```

Fill in `SESSION_SECRET`, `IP_HASH_SALT` and `ADMIN_PASSWORD` in `.env` first, and keep `COOKIE_SECURE=0`. The site runs at http://localhost:8080, and the admin page is at http://localhost:8080/admin.

Voting starts closed. Open it from the admin page.

New visitors see an animated basketball landing page. Start voting or Skip intro
opens the username form; the page does not redirect on a timer. The shot can be
replayed, reduced-motion preferences disable animation, and returning voters
continue directly at their saved progress.

The username screen introduces the draft room. Player voting uses a community
card design, animated price selection and card transitions, and inline milestones
every ten answered players. "Locked in" appears only after the server confirms a
save. Reduced-motion preferences disable the card and price animations.
Each card shows the player's stored current ADP price for
comparison. This uses the voting site's imported price snapshot, not live Flux prices.
Other accepts whole-dollar valuations from $0 to $100. A $0 answer is a price
vote and is included in statistics and exports; it is distinct from Don't know.

My votes / Edit a player opens a private searchable answer list during voting.
Use All players to jump to an unanswered player too. Saving from this view returns
to the list; Continue voting resumes at the next unanswered player. Completion
opens the same scouting report automatically, with counts, all saved answers,
and the ten biggest increases/decreases versus current ADP. Existing sessions
and resume links get this report from their saved answers without voting again.
Closed voting permits viewing the report but not editing. Admin-excluded answers
are labelled and stay excluded when edited.
Completed reports default to Your ADP: all players ordered by the voter's saved
price, highest first, with alphabetical ties. $0 is a ranked price; Don't know
and unanswered players are unpriced and remain at the bottom without a rank.
Your rank always refers to the full personal price list, even when filtering.
View my ADP clears search/filters and restores this order. It also works for
previously completed sessions and their existing resume links.

Returning voters are recognised by their six-month HttpOnly session cookie. The
browser also remembers its private resume path and can restore that session if
the cookie is lost but local storage remains. If both are gone or the voter uses
another browser, Already voted? accepts their private resume link. Reusing a
username without proof of the existing session returns a clear recovery prompt
instead of creating another voter or exposing that person's answers. The current
session can submit its own username again without creating a duplicate.

Admins can Copy resume link beside a voter or in their details. Choose the row
with the correct saved progress, and share the link only with its owner: it gives
access to that session. Existing duplicate rows are preserved for review rather
than merged or removed automatically. Deleted sessions cannot be recovered until
restored. Never change SESSION_SECRET during a live vote; it invalidates links.

View report beside a voter or in their details opens the same personal report
inside admin: counts, top ten increases/decreases, all players ranked by voter
price, search and difference filters. Reports work for completed, partial and
empty sessions, and label admin exclusions. This view is read-only and does not
create a voter session, impersonate the voter, or expose their private resume link.

## Admin workspace

Combined ADP ranks all players using included price votes across partial and
completed sessions. Median is the default ranking; switch to Average to rank by
the mean instead. Both prices, current price, selected-method difference, vote
count and coverage are shown. Each included session contributes its saved price
once per player, including $0. Don't know answers, deleted answers and removed
voter sessions do not contribute. No-vote players remain unranked at the bottom;
ties sort alphabetically. Low counts are labelled without excluding those
players. Search, coverage/difference filters, Refresh and minute updates apply.
This report does not update the live ADP sheet or any player's stored price.

The overview shows included answers, average survey progress, players with at least five price votes, review flags, and removed answers. Progress counts answered cards even when an admin later removes an answer; pricing statistics and activity charts exclude removed answers.

Click a summary card to open the corresponding player or voter filter. Player details show every included answer, a price distribution, and the middle 50% of price votes. Voter details explain review flags and distinguish included from removed answers. Flags never remove votes automatically; shared IP addresses do not establish that voters are the same person.

Use Delete beside a voter, or Delete voter in their detail page, to remove the
session and its included answers. Voter details show Difference as answer minus
current ADP price, with a dash for Don't know answers.
Empty sessions can be deleted too. A reason is
required. Removed sessions cannot vote or resume until restored. Restore the
session from Deleted answers; earlier individual exclusions stay excluded and
can be restored separately afterwards. Removing included answers alone remains
available in voter details. Exports exclude removed sessions and their answers.

Overview and list pages refresh every minute while visible. Use Refresh for an immediate update. After changing Python files, restart the server before refreshing `/admin`.

Use the Light mode / Dark mode button on the login page or dashboard to choose a theme. The choice is remembered in that browser; without a saved choice, the admin follows the device's system theme. Charts, tables, and review dialogs use the selected theme too.

## Run the tests

```
python -m unittest discover -s tests -v
```

## Deploy to Fly

```
fly apps create tfm-adp-pricing
fly volumes create tfm_pricing_data --region ewr --size 1
fly secrets set SESSION_SECRET=... IP_HASH_SALT=... ADMIN_PASSWORD=...
fly deploy
```

If the app name `tfm-adp-pricing` is taken, change `app` and `PUBLIC_BASE_URL` in `fly.toml`.

Optional features need extra secrets:

| Feature | Secrets | Setup |
| --- | --- | --- |
| Continue Later email | `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD` | Use a dedicated Gmail account with 2-Step Verification and an app password |
| Google Sheets export | `EXPORT_SPREADSHEET_ID`, `GOOGLE_SERVICE_ACCOUNT_JSON` | Create an empty spreadsheet and share it with the service account as an editor |

## Players

`players.json` holds the one-time copy of the players. To refresh it before launch, run `python import_players.py`. It stops if the sheet doesn't run from Michael Jordan to Ben Simmons across 313 rows.

The database copies `players.json` only when it has no players, so re-importing after launch doesn't change a running vote.

## Player photos

`player_photos.json` maps player names to photos, which visitors' browsers load directly from NBA.com or Wikimedia Commons. Wikipedia photos are limited to freely licensed images and show a credit link. Players with no entry show a basketball instead.

To fix a wrong photo, edit that player's entry and restart the server. Any new image host must also be added to `CSP` in `server.py`.

## Before opening the real vote

Run a test with a few drafters, then clear the test data on the server. This permanently removes all voters and votes:

```
fly ssh console -C "python reset_votes.py --yes"
```

## Good to know

- **Secrets:** changing `SESSION_SECRET` signs everyone out and breaks every resume link.
- **Backups:** export to Google Sheets before closing voting, and keep Fly volume snapshots on.
- **Scale:** the site uses SQLite on one volume, so keep it on one machine.
