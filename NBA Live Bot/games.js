const { AttachmentBuilder, EmbedBuilder, escapeMarkdown } = require('discord.js');
const teamEmojis = require('./team_emojis.json');
const EMOJI_ALIASES = { GSW: 'GS', NYK: 'NY', NOP: 'NO', UTA: 'UTAH', SAS: 'SA', WAS: 'WSH', PHO: 'PHX', BRK: 'BKN' };
function teamEmoji(team) {
  const abbreviation = String(team?.abbreviation || '').toUpperCase();
  return teamEmojis[abbreviation] || teamEmojis[EMOJI_ALIASES[abbreviation]] || '';
}
function countdownEmbeds(games, now = Date.now()) {
  const lines = (games || []).filter(game => game.state === 'pre' && !game.stopped && !/cancel|postpon|suspend/i.test(game.status || '')).map(game => {
    const label = team => escapeMarkdown(String(team?.abbreviation || team?.name || 'TBC').replace(/[\r\n]/g, ' ').slice(0, 65));
    const timestamp = Date.parse(game.date);
    const when = !Number.isFinite(timestamp) ? 'Time TBC' : /delay/i.test(game.status || '') ? 'Delayed'
      : timestamp <= Number(new Date(now)) ? 'Awaiting tip-off' : `Starts <t:${Math.floor(timestamp / 1000)}:R>`;
    const awayEmoji = teamEmoji(game.away), homeEmoji = teamEmoji(game.home);
    return `${awayEmoji ? `${awayEmoji} ` : ''}**${label(game.away)} @ ${label(game.home)}**${homeEmoji ? ` ${homeEmoji}` : ''} · ${when}`;
  });
  if (!lines.length) return [];
  return [new EmbedBuilder().setColor(0x2589EC).setTitle('Tip-off countdowns')
    .setDescription(lines.join('\n'))
    .setFooter({ text: 'Hover or tap a countdown for tip-off in your local time.' })];
}
function gamesPayload(slate, png) {
  return { content: '', embeds: countdownEmbeds(slate.games, slate.updatedAt || Date.now()), files: [new AttachmentBuilder(png, { name: 'nba-games.png', description: `${slate.games.length} live and upcoming NBA games` })], components: [], allowedMentions: { parse: [] } };
}
function schedulePayload(schedule, png) {
  const games = (schedule.upcoming || []).map(game => ({ ...game,
    away: game.home ? game.opponent : schedule.team, home: game.home ? schedule.team : game.opponent }));
  return { content: '', embeds: countdownEmbeds(games, schedule.updatedAt || Date.now()), files: [new AttachmentBuilder(png, { name: 'nba-schedule.png', description: `${schedule.team.name} schedule` })], components: [], allowedMentions: { parse: [] } };
}
module.exports = { gamesPayload, schedulePayload, countdownEmbeds, teamEmoji };
