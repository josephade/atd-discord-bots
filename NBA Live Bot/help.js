const { EmbedBuilder, ActionRowBuilder, ButtonBuilder, ButtonStyle } = require('discord.js');
const tabs = { overview: 'Start Here', players: 'Player Stats', games: 'Games', teams: 'Team Tools' };
const HELP_ID = 'nba-help:';

function helpPayload({ page = 'overview', prefix = '!', seconds = 30, avatar } = {}) {
  if (!Object.hasOwn(tabs, page)) page = 'overview';
  const example = (command, text) => `\`${prefix}${command}\`\n${text}`;
  const fields = {
    overview: [
      { name: '🏀 Player Cards', value: example('nba LeBron James', 'Live game stats, or his last played game until tip-off.') },
      { name: '📺 Tonight’s Games', value: example('nba games', 'Live scores and upcoming matchups.') },
      { name: '📊 Full Box Scores', value: example('nba boxscores', 'Choose a game, browse dates, and refresh the score.') },
    ],
    players: [
      { name: 'Game Card', value: example('nba LeBron James', 'Statline · Quarter by Quarter · Shot Breakdown') },
      { name: 'Season & Recent Form', value: [example('season LeBron James', 'Current regular-season averages.'), example('recent LeBron James', 'Last 10 games and averages.'), example('season LeBron James 2025-26 playoffs', 'Choose a season and competition.')].join('\n\n') },
      { name: 'Compare Players', value: example('nba compare LeBron James vs Stephen Curry', 'Season averages and team records side by side.') },
      { name: 'Live Watch', value: example('watch LeBron James', `Updates every ${seconds} seconds through the final buzzer.\nStop with \`${prefix}unwatch\`. Watches expire after 8 hours or a restart.`) },
      { name: 'Past Games', value: `\`${prefix}nba LeBron James 2026-10-30\`\nAlso accepts \`26/Dec\` or \`26/Dec/2025\`.\nNo appearance that day? Choose whether to view his closest recorded game; the prompt lasts 60 seconds.` },
    ],
    games: [
      { name: 'Live & Upcoming', value: example('nba games', 'See tonight’s NBA slate and current scores.') },
      { name: 'Box Scores', value: example('nba boxscores', 'Select a matchup to see starters, bench, and team totals.\nUse Previous Day / Next Day and Refresh.') },
      { name: 'Choose a Date', value: example('nba boxscores 2026-10-30', `Open that day’s games. \`${prefix}nba boxscore\` also works.`) },
      { name: 'Team Schedule', value: example('nba schedule Lakers', 'Upcoming games and recent results.') },
    ],
    teams: [
      { name: 'Injury Reports', value: example('nba injury Lakers', `Injury details and expected return.\nUse \`${prefix}nba injury\` to choose a team.`) },
      { name: 'Standings', value: example('nba standings', 'Conference standings with East / West buttons.') },
      { name: 'Salary & Cap Space', value: example('nba salary Lakers', 'Payroll, salary cap, tax, and aprons. Browse later seasons with the buttons.') },
      { name: 'Draft Picks', value: example('nba picks Lakers', `2027–2030 ownership, protections, and swaps.\n\`${prefix}nba pick Lakers\` also works.`) },
    ],
  };
  const embed = new EmbedBuilder().setColor(0x2589EC)
    .setAuthor({ name: 'NBA LIVE • COMMAND GUIDE', ...(avatar ? { iconURL: avatar } : {}) })
    .setTitle(tabs[page])
    .setDescription(page === 'overview' ? 'Your NBA courtside companion. Choose a category below.' : 'Copy an example below to get started.')
    .addFields(fields[page])
    .setFooter({ text: page === 'teams' ? 'ESPN data • Draft picks: dated RealGM snapshot • Feeds may be delayed' : 'Full player names or ESPN IDs work • Feeds may be delayed' });
  if (avatar && page === 'overview') embed.setThumbnail(avatar);
  const row = new ActionRowBuilder().addComponents(Object.entries(tabs).map(([key, label]) =>
    new ButtonBuilder().setCustomId(`${HELP_ID}${key}`).setLabel(label)
      .setStyle(key === page ? ButtonStyle.Primary : ButtonStyle.Secondary).setDisabled(key === page)));
  return { content: '', embeds: [embed], components: [row], allowedMentions: { parse: [] } };
}

module.exports = { helpPayload, HELP_ID };
