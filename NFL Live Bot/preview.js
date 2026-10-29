require('dotenv').config();
const fs = require('node:fs/promises');
const { NFLData, derived } = require('./data');
const { Renderer } = require('./render');
function demoModel() {
  const passing = { 'completions/passingAttempts': '22/35', passingYards: '275', yardsPerPassAttempt: '7.9', passingTouchdowns: '2', interceptions: '1', 'sacks-sackYardsLost': '2-14', QBRating: '98.5', adjQBR: '62.1' };
  const rushing = { rushingAttempts: '4', rushingYards: '18', yardsPerRushAttempt: '4.5', rushingTouchdowns: '0', longRushing: '9' };
  const categories = [
    { key: 'passing', label: 'Passing', labels: ['C/ATT', 'YDS', 'AVG', 'TD', 'INT', 'SACKS', 'QBR', 'RTG'], values: ['22/35', '275', '7.9', '2', '1', '2-14', '62.1', '98.5'] },
    { key: 'rushing', label: 'Rushing', labels: ['CAR', 'YDS', 'AVG', 'TD', 'LONG'], values: ['4', '18', '4.5', '0', '9'] },
  ];
  return {
    player: { displayName: 'Example Quarterback', team: { id: '1', displayName: 'Example team', color: '008348' }, position: { abbreviation: 'QB' } },
    state: 'in', status: 'Q3 · 4:22', matchup: 'DEMO 17 · OPP 13', period: 3,
    note: 'DEMO DATA · reference-style example, not a real game', primary: 'passing', categories,
    stats: derived({ ...passing, ...rushing }),
    drives: {
      available: true,
      teams: [{ id: '1', abbreviation: 'DEM', score: '17' }, { id: '2', abbreviation: 'OPP', score: '13' }],
      periods: [
        { period: 1, label: 'Q1', values: ['7', '3'] }, { period: 2, label: 'Q2', values: ['0', '7'] },
        { period: 3, label: 'Q3', values: ['7', '3'] }, { period: 4, label: 'Q4', values: ['—', '—'] },
      ],
      totals: ['17', '13'],
      drives: [
        { period: 1, teamId: '1', teamAbbreviation: 'DEM', isPlayerTeam: true, start: 'DEM 25', plays: 8, timeOfPossession: '4:12', yards: 55, isScore: true, result: 'Touchdown' },
        { period: 1, teamId: '2', teamAbbreviation: 'OPP', isPlayerTeam: false, start: 'OPP 30', plays: 5, timeOfPossession: '2:30', yards: 28, isScore: true, result: 'Field Goal' },
      ],
    },
  };
}
async function main() {
  const face = process.argv.find(arg => arg.startsWith('--page='))?.slice(7) || (process.argv.includes('--back') ? 'quarters' : 'front');
  if (!['front', 'quarters', 'drives'].includes(face)) throw new Error('Use --page=front, --page=quarters, or --page=drives.');
  const args = process.argv.slice(2).filter(arg => arg !== '--back' && !arg.startsWith('--page=')), data = new NFLData(), renderer = new Renderer();
  try {
    let model;
    if (args[0] === '--demo' || !args.length) {
      model = demoModel();
    } else {
      const { parse } = require('./commands');
      const cmd = parse(args.join(' '));
      const player = await data.findPlayer(cmd.query);
      model = cmd.command === 'season' ? await data.season(player, cmd.year, cmd.playoffs) : await data.game(player, cmd.date);
    }
    await fs.writeFile('preview.png', await renderer.render({ ...model, face }));
    console.log(`Saved preview.png: ${model.player.displayName} · ${model.status}`);
  } finally { await renderer.close(); }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
