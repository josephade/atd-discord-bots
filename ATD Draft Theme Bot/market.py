"""Confirmed purchases of shared anonymous suspect guesses."""
import json
import discord
import db
import suspects
from themes.base import draft_lock


async def channel_check(ctx):
    if not ctx.guild or ctx.channel.id not in suspects.ALLOWED_CHANNEL_IDS:
        await ctx.reply('Use `!market` and `!buys` in <#1566394824288894976> or the private admin channel. No wins were charged.',
                        mention_author=False, allowed_mentions=discord.AllowedMentions.none())
        return False
    return True


def team_users(draft, user_id):
    drafter = db.get_drafter_by_user(draft['id'], user_id)
    matches = [t for t in suspects.teams(draft) if drafter and drafter['id'] in t['member_ids']]
    if len(matches) != 1:
        raise ValueError('You need one assigned team in this anonymous draft to buy guesses.')
    return sorted(db.get_drafter(mid)['user_id'] for mid in matches[0]['member_ids'])


def resolve(ctx):
    if not ctx.guild:
        raise ValueError('Use the market in the draft server.')
    draft, error = suspects.resolve_draft(ctx.guild.id, ctx.channel.id, ctx.author.id)
    if error:
        raise ValueError(error)
    if draft['state'].get('identities_revealed'):
        raise ValueError('The GM identities have been revealed. Suspect purchases are closed.')
    return draft


async def quote(ctx, text):
    if not ctx.guild or ctx.channel.id not in suspects.ALLOWED_CHANNEL_IDS:
        raise ValueError('Use `!buys` in <#1566394824288894976> or the private admin channel. No wins were charged.')
    parts = text.split()
    if len(parts) != 2 or parts[0].casefold() != 'suspect' or not parts[1].isascii() or not parts[1].isdigit():
        raise ValueError('Use `!buys suspect <quantity>`. Example: `!buys suspect 2` costs **10 wins**.')
    quantity = int(parts[1])
    draft = resolve(ctx)
    async with draft_lock(draft['id']):
        draft = db.get_draft(draft['id'])
        if draft['status'] not in ('active', 'paused', 'complete') or draft['state'].get('identities_revealed'):
            raise ValueError('Suspect purchases are closed. No wins were charged.')
        return db.create_suspect_purchase(draft['id'], ctx.message.id, ctx.author.id, team_users(draft, ctx.author.id), quantity)


async def settle(purchase, actor_id, confirm):
    async with draft_lock(purchase['draft_id']):
        draft = db.get_draft(purchase['draft_id'])
        users = team_users(draft, actor_id) if confirm else []
        return db.finish_suspect_purchase(purchase['id'], actor_id, users, confirm)


class PurchaseConfirm(discord.ui.View):
    def __init__(self, purchase):
        super().__init__(timeout=300)
        self.purchase = purchase
        self.message = None

    async def interaction_check(self, interaction):
        if interaction.user.id != self.purchase['buyer_id']:
            await interaction.response.send_message('Only the GM who requested this purchase can use these buttons.', ephemeral=True)
            return False
        if interaction.channel_id not in suspects.ALLOWED_CHANNEL_IDS:
            await interaction.response.send_message('Start a new purchase in <#1566394824288894976> or the private admin channel. No wins were charged.', ephemeral=True)
            return False
        return True

    async def finish(self, interaction, confirm):
        await interaction.response.defer()
        try:
            result = await settle(self.purchase, interaction.user.id, confirm)
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        if result['status'] == 'confirmed':
            text = (f"**Purchase confirmed: {result['quantity']} extra suspect guess(es).**\n"
                    f"Your team loses **{result['cost']} wins**. The guesses are shared by your team.\n"
                    'Win deduction saved in the commissioner ledger for application to the results.')
        else:
            text = 'Purchase '+result['status']+'. **No wins charged and no guesses added.**'
        await interaction.edit_original_response(embed=discord.Embed(title='Suspect Market', description=text, color=0xD4AF37), view=None)
        self.stop()

    @discord.ui.button(label='Confirm purchase', style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        await self.finish(interaction, True)

    @discord.ui.button(label='Cancel', style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        await self.finish(interaction, False)

    async def on_timeout(self):
        await settle(self.purchase, self.purchase['buyer_id'], False)
        if self.message:
            try:
                await self.message.edit(embed=discord.Embed(title='Purchase expired', description='No wins charged and no guesses added.'), view=None)
            except discord.HTTPException:
                pass


async def buy(ctx, text):
    purchase = await quote(ctx, text)
    if purchase['status'] != 'pending':
        await ctx.reply('This purchase is already '+purchase['status']+'. No additional wins were charged.', mention_author=False)
        return
    view = PurchaseConfirm(purchase)
    description = (f"Buy **{purchase['quantity']} extra suspect guess(es)** for **{purchase['cost']} wins**?\n\n"
                   '**5 wins per guess.** Your team loses the full amount if you confirm.\n'
                   'Maximum **10 purchased guesses per team per draft**, shared by partners. This cost is separate from any penalty for being caught.\n'
                   'Confirm within 5 minutes, or cancel without charge.')
    view.message = await ctx.reply(embed=discord.Embed(title='Confirm suspect purchase', description=description, color=0xD4AF37),
                                   view=view, mention_author=False, allowed_mentions=discord.AllowedMentions.none())
