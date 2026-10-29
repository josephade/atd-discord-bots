const { AttachmentBuilder } = require('discord.js');
function gamesPayload(slate, png) {
  return { content: '', files: [new AttachmentBuilder(png, { name: 'nfl-games.png', description: `${slate.games.length} live and upcoming NFL games` })], components: [], allowedMentions: { parse: [] } };
}
module.exports = { gamesPayload };
