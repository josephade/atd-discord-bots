const { ActionRowBuilder, ButtonBuilder, ButtonStyle, ComponentType } = require('discord.js');
async function suggestGame({ reply, userId, player, date, suggestion, data, renderer, payload, showCard, timeout = 60000 }) {
  if (!suggestion) {
    await reply.edit({ content: `${player.displayName} has no recorded appearance on ${date}, and no alternative appearance was found in the available game logs.`, components: [], allowedMentions: { parse: [] } });
    return;
  }
  const row = new ActionRowBuilder().addComponents(
    new ButtonBuilder().setCustomId('nfl-date-yes').setLabel(`Yes, show ${suggestion.date}`).setStyle(ButtonStyle.Primary),
    new ButtonBuilder().setCustomId('nfl-date-no').setLabel('No thanks').setStyle(ButtonStyle.Secondary),
  );
  await reply.edit({ content: `${player.displayName} has no recorded appearance on **${date}**. Their closest recorded game is **${suggestion.date}**. Would you like that box score instead?`, components: [row], allowedMentions: { parse: [] } });
  let interaction;
  try {
    interaction = await reply.awaitMessageComponent({ componentType: ComponentType.Button, time: timeout, filter: i => {
      if (i.user.id === userId) return true;
      void i.reply({ content: 'Only the person who requested this box score can choose.', flags: 64 }).catch(() => {});
      return false;
    } });
  } catch {
    await reply.edit({ content: `Suggestion expired. To request it, use !nfl ${player.displayName} ${suggestion.date}.`, components: [], allowedMentions: { parse: [] } });
    return;
  }
  await interaction.deferUpdate();
  if (interaction.customId === 'nfl-date-no') {
    await reply.edit({ content: 'Okay, no alternative game selected.', components: [] });
    return;
  }
  await reply.edit({ content: `Loading ${player.displayName}'s box score for ${suggestion.date}…`, components: [] });
  const model = await data.game(player, null, suggestion.eventId);
  if (!model.played) throw new Error('That game no longer has a recorded appearance for this player. Please try another date.');
  if (showCard) await showCard(reply, model);
  else await reply.edit({ ...payload(model, await renderer.render(model)), components: [] });
}
async function suggestPlayer({ reply, userId, query, suggestions, timeout = 60000 }) {
  const choices = suggestions.slice(0, 3);
  const row = new ActionRowBuilder().addComponents(
    ...choices.map((player, i) => new ButtonBuilder().setCustomId(`nfl-player-${i}`).setLabel(`Yes, ${player.displayName}`.slice(0, 80)).setStyle(ButtonStyle.Primary)),
    new ButtonBuilder().setCustomId('nfl-player-no').setLabel('No thanks').setStyle(ButtonStyle.Secondary),
  );
  await reply.edit({ content: choices.length === 1
    ? `No exact match for “${query}”. Did you mean **${choices[0].displayName}**?`
    : `No exact match for “${query}”. Did you mean one of these players?\n${choices.map(p => `• ${p.displayName}`).join('\n')}`,
    components: [row], allowedMentions: { parse: [] } });
  let interaction;
  try {
    interaction = await reply.awaitMessageComponent({ componentType: ComponentType.Button, time: timeout, filter: i => {
      if (i.user.id === userId) return true;
      void i.reply({ content: 'Only the person who requested these stats can choose.', flags: 64 }).catch(() => {});
      return false;
    } });
  } catch {
    await reply.edit({ content: 'Player suggestion expired. Run the command again with the correct player name.', components: [] });
    return null;
  }
  await interaction.deferUpdate();
  const match = interaction.customId.match(/^nfl-player-(\d)$/);
  const selected = match && choices[Number(match[1])];
  if (!selected) {
    await reply.edit({ content: 'Okay, no player selected. Try their full name.', components: [] });
    return null;
  }
  await reply.edit({ content: `Looking up NFL stats for ${selected.displayName}…`, components: [] });
  return selected;
}
module.exports = { suggestGame, suggestPlayer };
