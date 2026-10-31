"""ATD voting receipts. Run --preview for an offline CSV image."""
import argparse
import asyncio
import csv
import io
import json
import logging
import os
from pathlib import Path

from votes import Ballot, normalize, parse_rows, resolve_member
from render import render_tierlist

log = logging.getLogger('atd.voting')
VOTER_CHANNEL_IDS = {934052158532378634, 1471629828208988314, 934054851485270016, 1533242104304631969}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--preview', type=Path, help='CSV file; does not connect to Discord or Google')
    parser.add_argument('--row', type=int, default=1, help='Ballot number in CSV, starting at 1')
    parser.add_argument('--draft', default='Preview')
    parser.add_argument('--tiers', type=int, default=10)
    parser.add_argument('--output', type=Path, default=Path('outputs/tierlist-preview.png'))
    args = parser.parse_args()
    if args.preview:
        with args.preview.open(encoding='utf-8-sig', newline='') as f:
            ballots = parse_rows(list(csv.reader(f)), require_name=False)
        if not 1 <= args.row <= len(ballots):
            parser.error(f'Choose a ballot between 1 and {len(ballots)}.')
        ballot = ballots[args.row-1]
        if ballot.error:
            parser.error(ballot.error)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(render_tierlist(args.draft, ballot, args.tiers))
        print(f'Preview saved: {args.output.resolve()} ({len(ballot.teams)} teams)')
        return
    from instance_lock import single_instance
    try:
        with single_instance(Path(__file__).resolve().parent/'state'/'bot.lock'):
            bot = create_bot()
            bot.run(os.environ['DISCORD_TOKEN'])
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc


def create_bot():
    import discord
    from discord.ext import commands, tasks
    from dotenv import load_dotenv
    from sheets import Sheets, sheet_reference
    from storage import Store

    env_path = Path(__file__).resolve().with_name('.env')
    # A local .env is authoritative; deployment secrets still work without it.
    load_dotenv(env_path, override=True, encoding='utf-8-sig')
    token = (os.getenv('DISCORD_TOKEN') or '').strip()
    guild_value = (os.getenv('DISCORD_GUILD_ID') or '').strip()
    missing = [key for key, value in (('DISCORD_TOKEN', token), ('DISCORD_GUILD_ID', guild_value)) if not value]
    if missing:
        raise SystemExit(f"Missing {', '.join(missing)}. Checked {env_path}. Save the file and restart the bot.")
    if not guild_value.isdecimal() or int(guild_value) <= 0:
        raise SystemExit(f'DISCORD_GUILD_ID must be a positive numeric server ID. Checked {env_path}.')
    guild_id = int(guild_value)
    dry_run = os.getenv('DRY_RUN', 'true').lower() != 'false'
    state_dir = Path(os.getenv('STATE_DIR', 'state'))
    name_column = os.getenv('NAME_COLUMN', 'Enter Discord Name')
    store, sheets = Store(state_dir/'voting.db'), Sheets()
    lock = asyncio.Lock()
    intents = discord.Intents.default()
    intents.members = True
    intents.message_content = True

    class VotingBot(commands.Bot):
        async def setup_hook(self):
            watch.start()

        async def close(self):
            watch.cancel()
            await super().close()

    bot = VotingBot(command_prefix='!', intents=intents, help_command=None)

    class VoterChannelError(commands.CheckFailure):
        pass

    @bot.check
    async def voter_channel(ctx):
        if ctx.command.name not in {'mytierlist', 'votehelp', 'tierlist'} or ctx.guild is None:
            return True
        if ctx.guild.id == guild_id and ctx.channel.id in VOTER_CHANNEL_IDS:
            return True
        raise VoterChannelError()

    def commissioner():
        async def predicate(ctx):
            return bool(ctx.guild and ctx.guild.id == guild_id and
                        (ctx.author.guild_permissions.administrator or
                         any(r.name == 'LeComissioner' for r in ctx.author.roles)))
        return commands.check(predicate)

    async def notify(text):
        log.warning(text)
        channel_id = int(os.getenv('ADMIN_CHANNEL_ID') or '0')
        channel = bot.get_channel(channel_id)
        if channel and getattr(channel, 'guild', None) and channel.guild.id == guild_id:
            try:
                await channel.send(text[:1900], allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                log.exception('Could not notify commissioner channel')

    async def process(track, guild):
        rows = await asyncio.to_thread(sheets.read, track['sheet'], track['gid'])
        store.ingest(track['draft'], parse_rows(rows, name_column))
        for item in store.ready(track['draft']):
            ballot = Ballot(**json.loads(item['payload']))
            member = resolve_member(ballot.name, guild.members, store.aliases())
            if member is None:
                if item['status'] != 'unmatched':
                    await notify(f"ATD {track['draft']}: cannot uniquely match {ballot.name!r}. Use !votealias @member <form name>.")
                store.set_status(track['draft'], ballot.key, 'unmatched', 'No unique member', delay=300)
                continue
            png = await asyncio.to_thread(render_tierlist, track['draft'], ballot, track['tiers'])
            if dry_run:
                target = state_dir/'previews'/f"{track['draft']}-{ballot.key}.png"
                target.parent.mkdir(parents=True, exist_ok=True)
                if not target.exists():
                    target.write_bytes(png)
                    log.info('DRY RUN: would DM member %s; preview %s', member.id, target)
                # Leave pending: a live run will still deliver this ballot.
                store.set_status(track['draft'], ballot.key, 'pending', 'Dry-run preview', delay=300)
                continue
            # A crash between send and recording success is ambiguous. Never automatically
            # resend such records; commissioner can inspect the DM and decide what to do.
            store.set_status(track['draft'], ballot.key, 'sending', str(member.id))
            try:
                message = await member.send(
                    f"Your ATD {track['draft']} tier list — your original voting selections.",
                    file=discord.File(io.BytesIO(png), filename=f"ATD-{track['draft']}-tierlist.png"),
                    allowed_mentions=discord.AllowedMentions.none())
            except discord.Forbidden:
                store.set_status(track['draft'], ballot.key, 'blocked', str(member.id))
                await notify(f"ATD {track['draft']}: DMs are blocked for {ballot.name!r}. After they enable DMs, use !voteretry {track['draft']}.")
            except discord.HTTPException as exc:
                # Server errors/timeouts may occur after Discord accepted the message.
                status = 'uncertain' if exc.status >= 500 else 'retry'
                store.set_status(track['draft'], ballot.key, status, str(member.id), delay=300)
                await notify(f"ATD {track['draft']}: delivery {status} for {ballot.name!r} (HTTP {exc.status}).")
            except (OSError, asyncio.TimeoutError):
                store.set_status(track['draft'], ballot.key, 'uncertain', str(member.id))
                await notify(f"ATD {track['draft']}: uncertain delivery for {ballot.name!r}; check before resending.")
            else:
                store.set_status(track['draft'], ballot.key, 'sent', str(member.id), message_id=message.id)
                log.info('Delivered ATD %s to member %s', track['draft'], member.id)

    @tasks.loop(seconds=max(10, int(os.getenv('POLL_SECONDS', '30'))))
    async def watch():
        guild = bot.get_guild(guild_id)
        if guild is None:
            log.error('Bot is not in configured guild %s', guild_id)
            return
        async with lock:
            for track in store.tracks():
                try:
                    await process(track, guild)
                except Exception:
                    log.exception('Polling failed for ATD %s; will try next cycle', track['draft'])

    @watch.before_loop
    async def before_watch():
        await bot.wait_until_ready()
        guild = bot.get_guild(guild_id)
        if guild and not guild.chunked:
            await guild.chunk(cache=True)
        for row in store.db.execute("SELECT draft,key FROM deliveries WHERE status='sending'").fetchall():
            store.set_status(row['draft'], row['key'], 'uncertain', 'Interrupted during send')
        log.info('Ready; DRY_RUN=%s; %s tracked drafts', dry_run, len(store.tracks()))

    @bot.command()
    @commissioner()
    async def votetrack(ctx, draft: str, sheet_url: str, tiers: int = 10):
        """!votetrack 112 <response-sheet-url> [tier-count]"""
        if not draft.isdigit() or not 1 <= tiers <= 50:
            await ctx.send('Use a numeric draft and tier count between 1 and 50.')
            return
        async with lock:
            if store.db.execute('SELECT 1 FROM tracks WHERE draft=?', (draft,)).fetchone():
                await ctx.send('That draft is already registered. Use !votestatus or !voteresume.')
                return
            try:
                sheet, gid = sheet_reference(sheet_url.strip('<>'))
            except ValueError as exc:
                await ctx.send(str(exc))
                return
            if gid is None:
                gid = await asyncio.to_thread(sheets.resolve_gid, sheet)
            rows = await asyncio.to_thread(sheets.read, sheet, gid)
            try:
                ballots = parse_rows(rows, name_column)
            except ValueError as exc:
                await ctx.send(f'{exc} Open the response tab and copy its URL (including gid) if it is not the first tab.')
                return
            store.register(draft, sheet, gid, tiers, ballots)
        await ctx.send(f'ATD {draft} registered. {len(ballots)} existing responses marked historical. New votes will be processed. Dry run: {dry_run}.')

    @bot.command()
    @commissioner()
    async def votealias(ctx, member: discord.Member, *, form_name: str):
        if member.bot or not normalize(form_name):
            await ctx.send('Choose a person and a nonempty form name.')
            return
        async with lock:
            store.alias(normalize(form_name), member.id)
        await ctx.send('Name mapping saved. Unmatched responses will be checked again.')

    @bot.command()
    @commissioner()
    async def votestatus(ctx):
        lines = [f'Dry run: {dry_run}']
        lines += [f"ATD {r['draft']}: {r['status']} = {r['n']}" for r in store.counts()]
        lines += [f"ATD {t['draft']}: active" for t in store.tracks()]
        for start in range(0, len(lines), 25):
            await ctx.send('\n'.join(lines[start:start+25]), allowed_mentions=discord.AllowedMentions.none())

    @bot.command()
    @commissioner()
    async def votematches(ctx, draft: str):
        guild = bot.get_guild(guild_id)
        if guild is None:
            await ctx.send('The configured ATD server is unavailable.')
            return
        records = store.receipt_candidates(draft)
        if not records:
            await ctx.send('No saved responses found for that draft.')
            return
        aliases = store.aliases()
        names = {}
        for record in records:
            ballot = json.loads(record['payload'])
            name = ballot['name']
            names.setdefault(normalize(name), name)
        unmatched = [name for name in names.values()
                     if resolve_member(name, guild.members, aliases) is None]
        if not unmatched:
            await ctx.send(f'ATD {draft}: all {len(names)} form names match a Discord member.')
            return
        await ctx.send(f'ATD {draft}: {len(unmatched)} of {len(names)} form names need an alias.\n'
                       'Use `!votealias @Member <exact form name>` for each person.',
                       allowed_mentions=discord.AllowedMentions.none())
        for name in sorted(unmatched, key=normalize):
            await ctx.send(f'Form name: {name or "(blank — voter needs to enter a name)"}',
                           allowed_mentions=discord.AllowedMentions.none())

    @bot.command()
    @commissioner()
    async def voteissues(ctx, draft: str):
        records = store.db.execute("SELECT key,payload,status,detail FROM deliveries WHERE draft=? AND status IN ('invalid','unmatched','blocked','retry','uncertain','sending')", (draft,)).fetchall()
        if not records:
            await ctx.send('No delivery issues recorded for that draft.')
        for record in records:
            ballot = json.loads(record['payload'])
            text = f"{ballot['name']} | {ballot['timestamp']} | {record['status']}\n{ballot['error'] or record['detail']}"
            await ctx.send(text[:1900], allowed_mentions=discord.AllowedMentions.none())

    @bot.command()
    @commissioner()
    async def voteretry(ctx, draft: str):
        async with lock:
            store.retry(draft)
        await ctx.send('Blocked, unmatched, and retryable responses queued. Sent/historical/uncertain responses remain untouched.')

    @bot.command()
    @commissioner()
    async def votepause(ctx, draft: str):
        async with lock:
            with store.db:
                store.db.execute('UPDATE tracks SET active=0 WHERE draft=?', (draft,))
        await ctx.send(f'ATD {draft} paused.')

    @bot.command()
    @commissioner()
    async def voteresume(ctx, draft: str):
        async with lock:
            with store.db:
                store.db.execute('UPDATE tracks SET active=1 WHERE draft=?', (draft,))
        await ctx.send(f'ATD {draft} resumed; unseen submissions will be processed.')

    @bot.command()
    async def mytierlist(ctx, draft: str):
        if ctx.guild and ctx.guild.id != guild_id:
            return
        guild = bot.get_guild(guild_id)
        if guild is None or not any(m.id == ctx.author.id for m in guild.members):
            await ctx.send('You must be a member of the ATD server to retrieve a tier list.')
            return
        ballot = None
        for row in store.receipt_candidates(draft):
            candidate = Ballot(**json.loads(row['payload']))
            if candidate.error:
                continue
            if row['status'] == 'sent':
                belongs_to_user = row['detail'] == str(ctx.author.id)
            else:
                member = resolve_member(candidate.name, guild.members, store.aliases())
                belongs_to_user = member is not None and member.id == ctx.author.id
            if belongs_to_user:
                ballot = candidate
                break
        if ballot is None:
            await ctx.send('No saved vote uniquely matches your account in that draft. Ask a commissioner to map your form name with !votealias.')
            return
        png = await asyncio.to_thread(render_tierlist, draft, ballot, store.tier_count(draft))
        await ctx.send(file=discord.File(io.BytesIO(png), filename=f'ATD-{draft}-tierlist.png'),
                       allowed_mentions=discord.AllowedMentions.none())

    @bot.command()
    async def tierlist(ctx, *, query: str = ''):
        from team_report import report_from_rows, match_team, team_emoji_candidates
        from report_image import render_team_report, nba_logo
        import re
        if not query.strip():
            await ctx.send('Choose a team emoji or name. Example: `!tierlist 113 :PORF:`. You can omit the draft: `!tierlist :lakers:`.')
            return
        guild = bot.get_guild(guild_id)
        if guild is None or (ctx.guild and ctx.guild.id != guild_id) or not any(m.id == ctx.author.id for m in guild.members):
            await ctx.send('You must be a member of the ATD server to view voting reports.')
            return
        if query.strip().isdigit():
            await ctx.send('Add a team emoji or name after the draft. Example: `!tierlist 113 :PORF:`.')
            return
        match = re.fullmatch(r'(\d+)\s+(.+)', query.strip())
        if match:
            draft, query = match.groups()
        else:
            tracks = store.tracks()
            draft = max((t['draft'] for t in tracks if t['draft'].isdigit()), key=int, default=None)
        track = store.db.execute('SELECT * FROM tracks WHERE draft=?', (draft,)).fetchone() if draft else None
        if track is None:
            await ctx.send('That draft is not registered. Example: `!tierlist 112 :celtics:`.')
            return
        try:
            async with lock:
                rows = await asyncio.to_thread(sheets.read, track['sheet'], track['gid'])
        except Exception:
            log.exception('Could not read current voting responses for ATD %s', draft)
            await ctx.send('Could not read the current Google Form response sheet. Please try again shortly. No report was generated from old saved votes.')
            return
        try:
            report = report_from_rows(rows, query, track['tiers'], guild.members, store.aliases(), name_column)
        except ValueError as exc:
            await ctx.send(str(exc), allowed_mentions=discord.AllowedMentions.none())
            return
        logo = None
        emoji_match = re.search(r'<a?:[^:>]+:(\d+)>', query)
        emojis = getattr(guild, 'emojis', ())
        candidates = team_emoji_candidates(report['team'], emojis)
        if emoji_match:
            candidates.sort(key=lambda emoji: str(emoji.id) != emoji_match[1])
        for emoji in candidates[:2]:
            try:
                logo = await asyncio.wait_for(emoji.read(), timeout=5)
                break
            except (discord.HTTPException, OSError, asyncio.TimeoutError):
                continue
        if logo is None:
            logo = await asyncio.to_thread(nba_logo, report['team'])
        png = await asyncio.to_thread(render_team_report, draft, report, logo)
        await ctx.send(file=discord.File(io.BytesIO(png), filename=f'ATD-{draft}-team-votes.png'),
                       allowed_mentions=discord.AllowedMentions.none())

    @bot.command()
    @commissioner()
    async def revealgm(ctx, *, query: str = ''):
        """Explicit, draft-specific mappings for ambiguous sheet GM names."""
        import re
        from team_report import match_team
        from reveal import standings
        match = re.fullmatch(r'(\d+)\s+(.+?)\s+((?:<@!?\d+>\s*)+)', query.strip())
        if not match:
            await ctx.send('Use `!revealgm <ATD> <team emoji or name> @GM [@coGM]`. Example: `!revealgm 113 :lakers: @GM`.')
            return
        draft, team_query, mentions = match.groups()
        track = store.db.execute('SELECT * FROM tracks WHERE draft=?', (draft,)).fetchone()
        if track is None:
            await ctx.send('Register that ATD with `!votetrack` first.')
            return
        ids = list(dict.fromkeys(int(i) for i in re.findall(r'<@!?(\d+)>', mentions)))
        if any(not any(m.id == uid and not m.bot for m in ctx.guild.members) for uid in ids):
            await ctx.send('Mention actual GMs who are members of this server.')
            return
        try:
            async with lock:
                rows = await asyncio.to_thread(sheets.read, track['sheet'], track['gid'])
            team = match_team(team_query, [r['team'] for r in standings(rows)])
        except ValueError as exc:
            await ctx.send(str(exc), allowed_mentions=discord.AllowedMentions.none())
            return
        store.set_reveal_mapping(draft, team, ids)
        await ctx.send(f'Saved the GM mapping for ATD {draft} · {team}.', allowed_mentions=discord.AllowedMentions.none())

    @bot.command()
    @commissioner()
    async def revealsync(ctx, draft: str = ''):
        from reveal import standings, gm_ids
        track = store.db.execute('SELECT * FROM tracks WHERE draft=?', (draft,)).fetchone()
        if not draft.isdigit() or track is None:
            await ctx.send('Use `!revealsync <registered ATD>`, for example `!revealsync 113`, to save final standings for future GM comparisons.')
            return
        try:
            async with lock:
                rows = await asyncio.to_thread(sheets.read, track['sheet'], track['gid'])
            results = standings(rows)
        except ValueError as exc:
            await ctx.send(str(exc), allowed_mentions=discord.AllowedMentions.none())
            return
        for row in results:
            row['gm_ids'] = gm_ids(row, ctx.guild.members, store.gm_aliases(), store.reveal_mapping(draft, row['team']))
        store.save_reveal_results(draft, results, track['sheet'], track['gid'], replace=True)
        mapped = sum(bool(r['gm_ids']) for r in results)
        await ctx.send(f'Saved ATD {draft}: {len(results)} teams; {mapped} with verified Discord GM mappings. '
                       'Unmatched GM cells are retained but excluded from personal comparisons. Use `!revealgm` to map them. No reveals were posted.')

    @bot.command()
    @commissioner()
    async def reveal(ctx, *, query: str = ''):
        await start_reveal(ctx, query)

    @bot.command()
    @commissioner()
    async def revealall(ctx, draft: str = ''):
        await start_reveal(ctx, draft, all_seeds=True)

    active_reveal_channels = set()

    async def start_reveal(ctx, query, all_seeds=False):
        from reveal import standings, record_text, gm_ids, comparison, ordinal
        from nba_comparison import historical_comparison
        from team_report import report_from_rows, team_emoji_candidates
        from report_image import render_team_report, nba_logo
        parts = query.split()
        if len(parts) not in ((0, 1) if all_seeds else (1, 2)) or not all(p.isdigit() for p in parts):
            await ctx.send('Use `!revealall` for the current ATD or `!revealall 113`.' if all_seeds else 'Use `!reveal <seed>` for the current ATD, or `!reveal <ATD> <seed>`. Example: `!reveal 30` or `!reveal 113 29`.')
            return
        explicit_draft = bool(parts) if all_seeds else len(parts) == 2
        draft = parts[0] if explicit_draft else max((t['draft'] for t in store.tracks() if t['draft'].isdigit()), key=int, default=None)
        seed = None if all_seeds else int(parts[-1])
        track = store.db.execute('SELECT * FROM tracks WHERE draft=?', (draft,)).fetchone() if draft else None
        if track is None:
            await ctx.send('That ATD is not registered. Use `!votetrack` first.')
            return
        try:
            async with lock:
                rows = await asyncio.to_thread(sheets.read, track['sheet'], track['gid'])
            results = standings(rows)
            if not all_seeds and not 1 <= seed <= len(results):
                await ctx.send(f'ATD {draft} has {len(results)} teams. Choose a seed from 1 to {len(results)}.')
                return
            pending = []
            for result in (list(reversed(results)) if all_seeds else [results[seed-1]]):
                result['gm_ids'] = gm_ids(result, ctx.guild.members, store.gm_aliases(), store.reveal_mapping(draft, result['team']))
                report = report_from_rows(rows, result['team'], track['tiers'], ctx.guild.members, store.aliases(), name_column)
                pending.append((result, report))
        except ValueError as exc:
            await ctx.send(str(exc), allowed_mentions=discord.AllowedMentions.none())
            return
        except Exception:
            log.exception('Could not prepare reveal for ATD %s', draft)
            await ctx.send('Could not read the live voting sheet. Nothing was revealed. Try again shortly.')
            return
        async def post_result(result, report, show_gm):
            seed = result['seed']
            logo, emoji_text = None, result['team']
            for emoji in team_emoji_candidates(result['team'], getattr(ctx.guild, 'emojis', ())):
                emoji_text = f'{emoji} {result["team"]}'
                try:
                    logo = await asyncio.wait_for(emoji.read(), timeout=5)
                except (discord.HTTPException, OSError, asyncio.TimeoutError):
                    pass
                break
            if logo is None:
                logo = await asyncio.to_thread(nba_logo, result['team'])
            text = f"**ATD {draft} · {ordinal(seed)} Seed**\n{seed}. {emoji_text} **{record_text(result)}**"
            if show_gm:
                text += ' · ' + (' / '.join(f'<@{uid}>' for uid in result['gm_ids']) or discord.utils.escape_markdown(result['gm'] or 'GM not listed'))
                for uid in result['gm_ids']:
                    previous = store.previous_reveal(draft, uid)
                    text += '\n' + (comparison(result, previous, uid) if previous else f'<@{uid}>: no unambiguous earlier saved ATD result available for comparison.')
            text += '\n\n' + historical_comparison(result)
            png = await asyncio.to_thread(render_team_report, draft, report, logo, anonymous=not show_gm)
            await ctx.send(text, file=discord.File(io.BytesIO(png), filename=f'ATD-{draft}-seed-{seed}-votes.png'),
                           allowed_mentions=discord.AllowedMentions(everyone=False, roles=False,
                               users=[discord.Object(id=uid) for uid in result['gm_ids']] if show_gm else False,
                               replied_user=False))
            store.save_reveal_results(draft, [result], track['sheet'], track['gid'])
        notice_ready = asyncio.Event()
        notice = None

        async def remove_notice():
            await notice_ready.wait()
            if notice is not None:
                try:
                    await notice.delete()
                except discord.HTTPException:
                    log.warning('Could not delete reveal notice in channel %s', ctx.channel.id)

        class RevealChoice(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=180)
                self.used = False

            async def interaction_check(self, interaction):
                return interaction.user.id == ctx.author.id

            async def post(self, interaction, show_gm):
                if self.used:
                    return
                if ctx.channel.id in active_reveal_channels:
                    await interaction.response.send_message('A reveal is already running in that channel. Wait for it to finish before choosing again.')
                    return
                self.used = True
                active_reveal_channels.add(ctx.channel.id)
                for child in self.children:
                    child.disabled = True
                posted = 0
                try:
                    await interaction.response.edit_message(content='Posting your reveals from last seed to first…' if all_seeds else 'Posting your reveal…', view=self)
                    self.stop()
                    await remove_notice()
                    for index, (result, report) in enumerate(pending):
                        if index:
                            await asyncio.sleep(1.5)
                        await post_result(result, report, show_gm)
                        posted += 1
                except Exception:
                    log.exception('Reveal post failed')
                    await interaction.edit_original_response(content=f'Reveal stopped after {posted} confirmed posts. Check the channel before retrying; nothing will be retried automatically.', view=self)
                    return
                finally:
                    active_reveal_channels.discard(ctx.channel.id)
                summary = f'all {posted} seeds, from {len(results)} to 1' if all_seeds else f'{ordinal(seed)} Seed'
                await interaction.edit_original_response(content=f'Posted {summary} in <#{ctx.channel.id}>. Results saved for future GM comparisons.', view=self)

            @discord.ui.button(label='Reveal with GM', style=discord.ButtonStyle.primary)
            async def with_gm(self, interaction, button):
                await self.post(interaction, True)

            @discord.ui.button(label='Keep GM anonymous', style=discord.ButtonStyle.secondary)
            async def without_gm(self, interaction, button):
                await self.post(interaction, False)

            @discord.ui.button(label='Cancel', style=discord.ButtonStyle.danger)
            async def cancel(self, interaction, button):
                if self.used:
                    return
                self.used = True
                self.stop()
                await interaction.response.edit_message(content='Reveal cancelled. Nothing was posted.', view=None)
                await remove_notice()

        try:
            description = f'ATD {draft} · Reveal all {len(results)} seeds, from {len(results)} to 1' if all_seeds else f'ATD {draft} · {ordinal(seed)} Seed: {result["team"]} · {record_text(result)}'
            await ctx.author.send(description+'\n'
                                  f'Post in <#{ctx.channel.id}> with or without the GM? Buttons expire in 3 minutes.', view=RevealChoice())
        except discord.HTTPException:
            await ctx.send('I could not send your private reveal prompt. Enable DMs from this server and run the command again. Nothing was revealed.')
            return
        try:
            notice = await ctx.send('Check your DMs to choose whether to reveal the GM. Nothing is posted until you choose.')
        finally:
            notice_ready.set()

    @bot.command()
    async def votehelp(ctx):
        await ctx.send('Vote using the announcement Google Form. Your tier list arrives by DM.\n'
                       '`!mytierlist <draft>` — show your saved list here, including votes from before registration.\n'
                       '`!tierlist [draft] <team emoji or name>` — all voters\' tiers and standouts. Defaults to the latest active draft; test with `!tierlist 112 :celtics:`.\n'
                       'Commissioners: `!votetrack <draft> <response-sheet-url> [tiers]`, '
                       '`!reveal [ATD] <seed>` (private GM choice), `!revealsync <ATD>`, `!revealgm <ATD> <team> @GM [@coGM]`, '
                       '`!revealall [ATD]` (last seed to first, one private GM choice), '
                       '`!votealias @member <form name>`, `!votestatus`, `!voteretry <draft>`, '
                       '`!votematches <draft>`, `!voteissues <draft>`, `!votepause <draft>`, `!voteresume <draft>`.')

    @bot.event
    async def on_command_error(ctx, error):
        if isinstance(error, commands.CommandNotFound):
            return
        if isinstance(error, VoterChannelError):
            await ctx.send('Use this command in ' + ', '.join(f'<#{channel}>' for channel in sorted(VOTER_CHANNEL_IDS)) + ', or DM the bot.',
                           allowed_mentions=discord.AllowedMentions.none())
        elif isinstance(error, commands.CheckFailure):
            await ctx.send('Commissioner or administrator access in the ATD server is required.')
        elif isinstance(error, commands.UserInputError):
            await ctx.send('Check the command arguments. See !votehelp.')
        else:
            log.error('Command failed', exc_info=(type(error), error, error.__traceback__))
            await ctx.send('Command failed. Check the local log for details; no success was recorded.')

    bot.vote_watcher = watch
    bot.vote_store = store
    return bot


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    main()
