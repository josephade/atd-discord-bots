require('dotenv').config();
const fs = require('node:fs/promises');
const { NBAData, derived } = require('./data');
const { Renderer } = require('./render');
async function main() {
  const back = process.argv.includes('--back');
  const face = process.argv.find(arg => arg.startsWith('--page='))?.slice(7) || (back ? 'quarters' : 'front');
  if (!['front', 'quarters', 'shots'].includes(face)) throw new Error('Use --page=front, --page=quarters, or --page=shots.');
  const args = process.argv.slice(2).filter(arg => arg !== '--back' && !arg.startsWith('--page=')), data = new NBAData(), renderer = new Renderer();
  try {
    let model;
    if (args[0] === '--demo' || !args.length) {
      model = { player: { displayName: 'Paul George', team: { displayName: 'Example team', color: '008348' }, position: { abbreviation: 'F' } }, state: 'in', status: 'Q3 · 4:22', matchup: 'DEMO 78 · OPP 66', note: 'DEMO DATA · reference-style example, not a real game', stats: derived({ MIN: '21', PTS: '31', REB: '3', AST: '1', STL: '4', BLK: '0', '+/-': '+15', FG: '10-14', '3PT': '6-8', FT: '5-5', TO: '3', PF: '3', OREB: '0', DREB: '3' }) };
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
