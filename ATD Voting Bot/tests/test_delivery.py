import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord
from bot import create_bot
from test_votes import rows


class ConfigTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_bom_env_overrides_empty_inherited_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory)/'.env'
            env_path.write_text('DISCORD_TOKEN=offline-test-token\nDISCORD_GUILD_ID=123\n', encoding='utf-8-sig')
            with patch('bot.__file__', str(Path(directory)/'bot.py')), patch.dict(os.environ,
                    {'DISCORD_TOKEN': '', 'DISCORD_GUILD_ID': '', 'STATE_DIR': directory, 'PYTHON_DOTENV_DISABLED': 'false'}):
                bot = create_bot()
                await bot.close()
                bot.vote_store.db.close()

    async def test_missing_setting_error_identifies_file_without_token(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch('bot.__file__', str(Path(directory)/'bot.py')), patch.dict(os.environ,
                    {'DISCORD_TOKEN': 'offline-test-token', 'DISCORD_GUILD_ID': ''}):
                with self.assertRaises(SystemExit) as caught:
                    create_bot()
                self.assertIn('Missing DISCORD_GUILD_ID', str(caught.exception))
                self.assertIn('.env', str(caught.exception))
                self.assertNotIn('offline-test-token', str(caught.exception))


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'DISCORD_TOKEN': 'offline-test-token',
            'DISCORD_GUILD_ID': '123', 'ADMIN_CHANNEL_ID': '', 'STATE_DIR': self.temp.name,
            'DRY_RUN': 'false', 'NAME_COLUMN': 'Enter Discord Name'})
        self.env.start()
        self.dotenv = patch('dotenv.load_dotenv')
        self.dotenv.start()
        self.bot = create_bot()
        self.person = SimpleNamespace(id=456, name='soapz', display_name='Soapz',
            global_name=None, bot=False, send=AsyncMock(return_value=SimpleNamespace(id=999)))
        self.guild = SimpleNamespace(id=123, members=[self.person])
        self.bot.get_guild = lambda uid: self.guild
        self.ctx = SimpleNamespace(send=AsyncMock())
        self.read = patch('sheets.Sheets.read', return_value=rows())
        self.read_mock = self.read.start()
        self.logo = patch('report_image.nba_logo', return_value=None)
        self.logo.start()
        await self.bot.get_command('votetrack').callback(self.ctx, '112',
            'https://docs.google.com/spreadsheets/d/test/edit#gid=10', 12)
        self.read_mock.return_value = rows(timestamp='10/12/2026 13:00:00')

    async def asyncTearDown(self):
        await self.bot.close()
        self.bot.vote_store.db.close()
        self.read.stop()
        self.logo.stop()
        self.dotenv.stop()
        self.env.stop()
        self.temp.cleanup()

    async def poll(self):
        await self.bot.vote_watcher.coro()

    def status(self):
        return self.bot.vote_store.db.execute("SELECT status FROM deliveries WHERE status!='historical'").fetchone()[0]

    async def test_new_vote_sends_image_once_and_records_message(self):
        await self.poll()
        self.person.send.assert_awaited_once()
        self.assertEqual(self.status(), 'sent')
        attachment = self.person.send.call_args.kwargs['file']
        self.assertTrue(attachment.filename.endswith('.png'))
        self.assertEqual(self.bot.vote_store.latest('112', 456)['message_id'], 999)
        await self.poll()
        self.person.send.assert_awaited_once()

    async def prepare_reveal(self):
        ballot_rows = rows()
        ballot_rows[0][2] = '[Los Angeles Lakers]'
        self.read_mock.return_value = ballot_rows+[[], ['Team', 'Wins', 'Losses', 'GM'],
            ['Los Angeles Lakers', '50', '32', 'Soapz']]
        ctx = SimpleNamespace(guild=self.guild, author=self.person, channel=SimpleNamespace(id=789), send=AsyncMock())
        await self.bot.get_command('reveal').callback(ctx, query='112 1')
        return ctx, self.person.send.call_args.kwargs['view']

    async def test_reveal_private_choice_before_posting_anonymous_and_save_once(self):
        ctx, view = await self.prepare_reveal()
        self.assertNotIn('file', ctx.send.call_args.kwargs)
        self.assertEqual(self.bot.vote_store.db.execute('SELECT COUNT(*) FROM reveal_results').fetchone()[0], 0)
        interaction = SimpleNamespace(user=self.person, response=SimpleNamespace(edit_message=AsyncMock()), edit_original_response=AsyncMock())
        await view.children[1].callback(interaction)
        ctx.send.return_value.delete.assert_awaited_once()
        text = ctx.send.call_args.args[0]
        self.assertIn('50-32', text)
        self.assertNotIn('Soapz', text)
        self.assertNotIn('<@456>', text)
        self.assertIn('1st Seed', text)
        self.assertIn('50-32 and the 1st seed: a 50-win team', text)
        self.assertEqual(ctx.send.call_args.kwargs['allowed_mentions'].users, False)
        self.assertIn('file', ctx.send.call_args.kwargs)
        self.assertEqual(self.bot.vote_store.db.execute('SELECT COUNT(*) FROM reveal_results').fetchone()[0], 1)
        calls = ctx.send.await_count
        await view.children[0].callback(interaction)
        self.assertEqual(ctx.send.await_count, calls)

    async def test_reveal_with_gm_includes_previous_record_comparison(self):
        self.bot.vote_store.save_reveal_results('111', [dict(team='Los Angeles Lakers', wins='20', losses='62', gm_ids=[456])], 'test', 10)
        ctx, view = await self.prepare_reveal()
        interaction = SimpleNamespace(user=self.person, response=SimpleNamespace(edit_message=AsyncMock()), edit_original_response=AsyncMock())
        await view.children[0].callback(interaction)
        self.assertIn('<@456>', ctx.send.call_args.args[0])
        self.assertIn('50-32 and the 1st seed: a 50-win team', ctx.send.call_args.args[0])
        self.assertIn('improved by 30 wins compared with ATD 111', ctx.send.call_args.args[0])
        self.assertEqual([u.id for u in ctx.send.call_args.kwargs['allowed_mentions'].users], [456])
        self.assertNotIn('verified Discord mapping', ctx.send.call_args.args[0])

    async def test_reveal_invalid_seed_does_not_dm_or_write(self):
        from test_reveal import rows as standing_rows
        self.read_mock.return_value = standing_rows()
        ctx = SimpleNamespace(guild=self.guild, author=self.person, send=AsyncMock())
        await self.bot.get_command('reveal').callback(ctx, query='112 30')
        self.person.send.assert_not_awaited()
        self.assertIn('has 2 teams', ctx.send.call_args.args[0])

    async def test_reveal_cancel_does_not_post_or_save(self):
        ctx, view = await self.prepare_reveal()
        interaction = SimpleNamespace(response=SimpleNamespace(edit_message=AsyncMock()))
        calls = ctx.send.await_count
        await view.children[2].callback(interaction)
        self.assertEqual(ctx.send.await_count, calls)
        self.assertEqual(self.bot.vote_store.db.execute('SELECT COUNT(*) FROM reveal_results').fetchone()[0], 0)

    async def test_reveal_dm_blocked_does_not_post_result_or_save(self):
        self.person.send.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason='Forbidden'), 'DMs closed')
        self.read_mock.return_value = [['Timestamp', 'Enter Discord Name', '[Lakers]'],
            ['10/28/2026 12:00:00', 'Soapz', 'Tier 1'], [],
            ['Team', 'Wins', 'Losses', 'GM'], ['Lakers', '50', '32', 'Soapz']]
        ctx = SimpleNamespace(guild=self.guild, author=self.person, channel=SimpleNamespace(id=789), send=AsyncMock())
        await self.bot.get_command('reveal').callback(ctx, query='112 1')
        self.assertIn('Enable DMs', ctx.send.call_args.args[0])
        self.assertNotIn('file', ctx.send.call_args.kwargs)
        self.assertEqual(self.bot.vote_store.db.execute('SELECT COUNT(*) FROM reveal_results').fetchone()[0], 0)

    async def test_failed_public_reveal_does_not_save_history(self):
        ctx, view = await self.prepare_reveal()
        ctx.send.side_effect = OSError('offline')
        interaction = SimpleNamespace(response=SimpleNamespace(edit_message=AsyncMock()), edit_original_response=AsyncMock())
        await view.children[0].callback(interaction)
        self.assertEqual(self.bot.vote_store.db.execute('SELECT COUNT(*) FROM reveal_results').fetchone()[0], 0)

    async def test_reveal_requires_commissioner(self):
        ctx = SimpleNamespace(guild=self.guild, author=SimpleNamespace(guild_permissions=SimpleNamespace(administrator=False), roles=[]))
        self.assertFalse(await self.bot.get_command('reveal').checks[0](ctx))

    async def prepare_revealall(self, draft=''):
        self.read_mock.return_value = rows()+[[], ['Team', 'Wins', 'Losses', 'GM'],
            ['Lakers', '50', '32', 'Soapz'], ['Bulls', '22', '60', 'Soapz']]
        ctx = SimpleNamespace(guild=self.guild, author=self.person, channel=SimpleNamespace(id=789), send=AsyncMock())
        await self.bot.get_command('revealall').callback(ctx, draft)
        return ctx, self.person.send.call_args.kwargs['view']

    async def test_revealall_one_private_choice_posts_descending_with_full_reports(self):
        ctx, view = await self.prepare_revealall()
        self.person.send.assert_awaited_once()
        self.assertIn('from 2 to 1', self.person.send.call_args.args[0])
        self.assertNotIn('file', ctx.send.call_args.kwargs)
        interaction = SimpleNamespace(response=SimpleNamespace(edit_message=AsyncMock()), edit_original_response=AsyncMock())
        with patch('bot.asyncio.sleep', new_callable=AsyncMock):
            await view.children[0].callback(interaction)
        posts = [c for c in ctx.send.call_args_list if 'file' in c.kwargs]
        self.assertEqual([c.kwargs['file'].filename for c in posts], ['ATD-112-seed-2-votes.png', 'ATD-112-seed-1-votes.png'])
        self.assertTrue(all('<@456>' in c.args[0] for c in posts))
        self.assertEqual(self.bot.vote_store.db.execute('SELECT COUNT(*) FROM reveal_results').fetchone()[0], 2)
        self.assertIn('all 2 seeds', interaction.edit_original_response.call_args.kwargs['content'])

    async def test_revealall_anonymous_applies_to_every_image(self):
        from report_image import render_team_report
        interaction = SimpleNamespace(response=SimpleNamespace(edit_message=AsyncMock()), edit_original_response=AsyncMock())
        with patch('bot.asyncio.sleep', new_callable=AsyncMock), patch('report_image.render_team_report', wraps=render_team_report) as renderer:
            ctx, view = await self.prepare_revealall('112')
            await view.children[1].callback(interaction)
        self.assertEqual(renderer.call_count, 2)
        self.assertTrue(all(c.kwargs['anonymous'] for c in renderer.call_args_list))
        self.assertTrue(all('<@456>' not in c.args[0] for c in ctx.send.call_args_list if 'file' in c.kwargs))

    async def test_revealall_stops_on_failed_send_without_posting_higher_seeds(self):
        ctx, view = await self.prepare_revealall()
        ctx.send.side_effect = OSError('offline')
        interaction = SimpleNamespace(response=SimpleNamespace(edit_message=AsyncMock()), edit_original_response=AsyncMock())
        await view.children[0].callback(interaction)
        self.assertEqual(self.bot.vote_store.db.execute('SELECT COUNT(*) FROM reveal_results').fetchone()[0], 0)
        self.assertIn('stopped after 0', interaction.edit_original_response.call_args.kwargs['content'])
        self.assertEqual(ctx.send.call_args.kwargs['file'].filename, 'ATD-112-seed-2-votes.png')

    async def test_revealall_requires_commissioner(self):
        ctx = SimpleNamespace(guild=self.guild, author=SimpleNamespace(guild_permissions=SimpleNamespace(administrator=False), roles=[]))
        self.assertFalse(await self.bot.get_command('revealall').checks[0](ctx))

    async def test_team_report_read_only_and_explicit_historical_draft(self):
        before = [dict(r) for r in self.bot.vote_store.receipt_candidates('112')]
        ctx = SimpleNamespace(guild=self.guild, author=self.person, send=AsyncMock())
        await self.bot.get_command('tierlist').callback(ctx, query='112 :lakers:')
        self.assertEqual(ctx.send.call_args.kwargs['file'].filename, 'ATD-112-team-votes.png')
        self.assertNotIn('view', ctx.send.call_args.kwargs)
        self.assertEqual(before, [dict(r) for r in self.bot.vote_store.receipt_candidates('112')])
        self.person.send.assert_not_awaited()

    async def test_team_report_defaults_latest_active_draft(self):
        await self.bot.get_command('votetrack').callback(self.ctx, '113',
            'https://docs.google.com/spreadsheets/d/test/edit#gid=10', 12)
        ctx = SimpleNamespace(guild=self.guild, author=self.person, send=AsyncMock())
        await self.bot.get_command('tierlist').callback(ctx, query=':lakers:')
        self.assertEqual(ctx.send.call_args.kwargs['file'].filename, 'ATD-113-team-votes.png')
        self.person.send.assert_not_awaited()

    async def test_team_report_missing_argument_has_example(self):
        await self.bot.get_command('tierlist').callback(self.ctx)
        self.assertIn('!tierlist :lakers:', self.ctx.send.call_args.args[0])

    async def test_team_report_excludes_deleted_archived_voters_and_uses_current_ratings(self):
        old_rows = rows(timestamp='10/12/2026 14:00:00')
        old_rows[1][1] = 'Removed voter'
        old_rows[1][2] = 'Tier 10'
        from votes import parse_rows
        self.bot.vote_store.ingest('112', parse_rows(old_rows))
        before = [dict(r) for r in self.bot.vote_store.receipt_candidates('112')]
        self.read_mock.return_value = rows(timestamp='10/12/2026 13:00:00')
        self.read_mock.return_value[1][2] = 'Tier 4'
        ctx = SimpleNamespace(guild=self.guild, author=self.person, send=AsyncMock())
        with patch('report_image.render_team_report', return_value=b'png') as render:
            await self.bot.get_command('tierlist').callback(ctx, query='112 :lakers:')
        report = render.call_args.args[1]
        self.assertEqual([(v['name'], v['tier']) for v in report['votes']], [('Soapz', 4)])
        self.assertEqual(report['average'], 4)
        self.read_mock.assert_called_with('test', 10)
        self.assertEqual(before, [dict(r) for r in self.bot.vote_store.receipt_candidates('112')])
        self.person.send.assert_not_awaited()

    async def test_team_report_empty_current_sheet_never_falls_back_to_archive(self):
        self.read_mock.return_value = [rows()[0]]
        ctx = SimpleNamespace(guild=self.guild, author=self.person, send=AsyncMock())
        await self.bot.get_command('tierlist').callback(ctx, query='112 :lakers:')
        self.assertNotIn('file', ctx.send.call_args.kwargs)
        self.assertIn('No current responses', ctx.send.call_args.args[0])

    async def test_team_report_google_failure_never_falls_back_to_archive(self):
        self.read_mock.side_effect = OSError('Google unavailable')
        ctx = SimpleNamespace(guild=self.guild, author=self.person, send=AsyncMock())
        await self.bot.get_command('tierlist').callback(ctx, query='112 :lakers:')
        self.assertNotIn('file', ctx.send.call_args.kwargs)
        self.assertIn('Could not read the current Google Form response sheet', ctx.send.call_args.args[0])

    async def test_team_report_uses_server_team_logo_and_sends_one_png(self):
        import io
        from PIL import Image
        logo = io.BytesIO()
        Image.new('RGBA', (64, 64), '#552583').save(logo, format='PNG')
        http = SimpleNamespace(get_from_cdn=AsyncMock(return_value=logo.getvalue()))
        emoji = discord.Emoji(guild=self.guild, state=SimpleNamespace(http=http),
            data={'id': '123456789', 'name': 'Lakers'})
        self.assertIsInstance(emoji.url, str)
        self.guild.emojis = [emoji]
        ctx = SimpleNamespace(guild=self.guild, author=self.person, send=AsyncMock())
        await self.bot.get_command('tierlist').callback(ctx, query='112 <:Lakers:123456789>')
        http.get_from_cdn.assert_awaited_once_with(emoji.url)
        ctx.send.assert_awaited_once()
        self.assertNotIn('view', ctx.send.call_args.kwargs)
        attachment = ctx.send.call_args.kwargs['file']
        self.assertEqual(Image.open(attachment.fp).format, 'PNG')
        self.person.send.assert_not_awaited()

    async def test_team_report_logo_download_failure_still_sends_report(self):
        http = SimpleNamespace(get_from_cdn=AsyncMock(side_effect=OSError('CDN unavailable')))
        self.guild.emojis = [discord.Emoji(guild=self.guild, state=SimpleNamespace(http=http),
            data={'id': '123456789', 'name': 'Lakers'})]
        ctx = SimpleNamespace(guild=self.guild, author=self.person, send=AsyncMock())
        await self.bot.get_command('tierlist').callback(ctx, query='112 <:Lakers:123456789>')
        self.assertEqual(ctx.send.call_args.kwargs['file'].filename, 'ATD-112-team-votes.png')

    async def test_historical_voter_can_request_receipt_without_queuing_mass_delivery(self):
        ctx = SimpleNamespace(guild=self.guild, author=self.person, send=AsyncMock())
        await self.bot.get_command('mytierlist').callback(ctx, '112')
        ctx.send.assert_awaited_once()
        self.assertEqual(ctx.send.call_args.kwargs['file'].filename, 'ATD-112-tierlist.png')
        self.person.send.assert_not_awaited()
        statuses = [r['status'] for r in self.bot.vote_store.receipt_candidates('112')]
        self.assertEqual(statuses, ['historical'])
        self.assertEqual(self.bot.vote_store.ready('112'), [])

    async def test_requested_list_in_dm_replies_in_same_conversation(self):
        ctx = SimpleNamespace(guild=None, author=self.person, send=AsyncMock())
        await self.bot.get_command('mytierlist').callback(ctx, '112')
        ctx.send.assert_awaited_once()
        self.assertIn('file', ctx.send.call_args.kwargs)
        self.person.send.assert_not_awaited()

    async def test_other_member_cannot_request_someone_elses_historical_vote(self):
        other = SimpleNamespace(id=789, name='other', display_name='other', global_name=None, bot=False, send=AsyncMock())
        self.guild.members.append(other)
        ctx = SimpleNamespace(guild=self.guild, author=other, send=AsyncMock())
        await self.bot.get_command('mytierlist').callback(ctx, '112')
        other.send.assert_not_awaited()
        self.assertIn('No saved vote', ctx.send.call_args.args[0])

    async def test_name_audit_includes_historical_votes_and_respects_aliases(self):
        self.person.display_name = 'different'
        self.person.name = 'different'
        await self.bot.get_command('votematches').callback(self.ctx, '112')
        self.assertEqual(self.ctx.send.call_args.args[0], 'Form name: Soapz')
        self.person.send.assert_not_awaited()
        self.bot.vote_store.alias('soapz', self.person.id)
        await self.bot.get_command('votematches').callback(self.ctx, '112')
        self.assertIn('all 1 form names match', self.ctx.send.call_args.args[0])

    async def test_closed_dms_wait_for_explicit_retry(self):
        response = SimpleNamespace(status=403, reason='Forbidden')
        self.person.send.side_effect = discord.Forbidden(response, 'DMs closed')
        await self.poll()
        self.assertEqual(self.status(), 'blocked')
        await self.poll()
        self.person.send.assert_awaited_once()
        self.person.send.side_effect = None
        await self.bot.get_command('voteretry').callback(self.ctx, '112')
        await self.poll()
        self.assertEqual(self.status(), 'sent')

    async def test_ambiguous_name_waits_for_alias(self):
        self.guild.members.append(SimpleNamespace(id=789, name='other', display_name='Soapz', global_name=None, bot=False))
        await self.poll()
        self.assertEqual(self.status(), 'unmatched')
        self.person.send.assert_not_awaited()
        await self.bot.get_command('votealias').callback(self.ctx, self.person, form_name='Soapz')
        await self.poll()
        self.assertEqual(self.status(), 'sent')

    async def test_invalid_ballot_never_sends(self):
        self.read_mock.return_value[1][2] = ''
        await self.poll()
        self.assertEqual(self.status(), 'invalid')
        self.person.send.assert_not_awaited()

    async def test_registration_without_gid_resolves_and_saves_first_tab_id(self):
        with patch('sheets.Sheets.resolve_gid', return_value=876) as resolve:
            await self.bot.get_command('votetrack').callback(self.ctx, '113',
                'https://docs.google.com/spreadsheets/d/test/edit', 12)
        resolve.assert_called_once_with('test')
        track = next(t for t in self.bot.vote_store.tracks() if t['draft'] == '113')
        self.assertEqual(track['gid'], 876)

    async def test_invalid_link_shows_message_without_registering(self):
        await self.bot.get_command('votetrack').callback(self.ctx, '113', 'not-a-sheet', 12)
        self.assertIn('Google Sheets URL', self.ctx.send.call_args.args[0])
        self.assertEqual(len(self.bot.vote_store.tracks()), 1)

    async def test_uncertain_server_error_not_automatically_retried(self):
        self.person.send.side_effect = discord.HTTPException(SimpleNamespace(status=502, reason='Bad Gateway'), 'server error')
        await self.poll()
        self.assertEqual(self.status(), 'uncertain')
        await self.bot.get_command('voteretry').callback(self.ctx, '112')
        await self.poll()
        self.person.send.assert_awaited_once()

    async def test_dry_run_saves_preview_without_sending(self):
        await self.bot.close()
        self.bot.vote_store.db.close()
        os.environ['DRY_RUN'] = 'true'
        self.bot = create_bot()
        self.bot.get_guild = lambda uid: self.guild
        await self.poll()
        self.person.send.assert_not_awaited()
        self.assertEqual(self.status(), 'pending')
        from pathlib import Path
        self.assertEqual(len(list((Path(self.temp.name)/'previews').glob('*.png'))), 1)

    async def test_commissioner_check_rejects_other_guild_and_regular_members(self):
        check = self.bot.get_command('votetrack').checks[0]
        author = SimpleNamespace(guild_permissions=SimpleNamespace(administrator=False), roles=[])
        self.assertFalse(await check(SimpleNamespace(guild=self.guild, author=author)))
        author.guild_permissions.administrator = True
        self.assertTrue(await check(SimpleNamespace(guild=self.guild, author=author)))
        self.assertFalse(await check(SimpleNamespace(guild=SimpleNamespace(id=999), author=author)))

    async def test_voter_commands_allowed_channels_and_dm(self):
        command = self.bot.get_command('mytierlist')
        for channel_id in (934052158532378634, 1471629828208988314, 934054851485270016, 1533242104304631969):
            ctx = SimpleNamespace(command=command, guild=self.guild, channel=SimpleNamespace(id=channel_id))
            self.assertTrue(await self.bot.can_run(ctx))
        self.assertTrue(await self.bot.can_run(SimpleNamespace(command=command, guild=None)))
        with self.assertRaises(discord.ext.commands.CheckFailure):
            await self.bot.can_run(SimpleNamespace(command=command, guild=self.guild, channel=SimpleNamespace(id=1)))
        with self.assertRaises(discord.ext.commands.CheckFailure):
            await self.bot.can_run(SimpleNamespace(command=command, guild=SimpleNamespace(id=999), channel=SimpleNamespace(id=934052158532378634)))
        self.assertTrue(await self.bot.can_run(SimpleNamespace(command=self.bot.get_command('votematches'), guild=self.guild, channel=SimpleNamespace(id=1))))
