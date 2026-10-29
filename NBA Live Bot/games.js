const { AttachmentBuilder } = require('discord.js');
function gamesPayload(slate, png) {
  return { content: '', files: [new AttachmentBuilder(png, { name: 'nba-games.png', description: `${slate.games.length} live and upcoming NBA games` })], components: [], allowedMentions: { parse: [] } };
}
function schedulePayload(schedule, png) {
  return { content: '', files: [new AttachmentBuilder(png, { name: 'nba-schedule.png', description: `${schedule.team.name} schedule` })], components: [], allowedMentions: { parse: [] } };
}
module.exports = { gamesPayload, schedulePayload };
