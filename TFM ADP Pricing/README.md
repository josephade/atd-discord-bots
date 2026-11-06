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

## Admin workspace

The overview shows included answers, average survey progress, players with at least five price votes, review flags, and removed answers. Progress counts answered cards even when an admin later removes an answer; pricing statistics and activity charts exclude removed answers.

Click a summary card to open the corresponding player or voter filter. Player details show every included answer, a price distribution, and the middle 50% of price votes. Voter details explain review flags and distinguish included from removed answers. Flags never remove votes automatically; shared IP addresses do not establish that voters are the same person.

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
