const { test } = require('node:test');
const assert = require('node:assert/strict');
const { parse } = require('../commands');
const { NBAData } = require('../data');
const { compareHTML, leader } = require('../compare_layout');

const roster = [
  { id: 'lbj', displayName: 'LeBron James' }, { id: 'sc', displayName: 'Stephen Curry' },
  { id: 'wemby', displayName: 'Victor Wembanyama' }, { id: 'jj', displayName: 'James Johnson' },
];
const data = () => { const d = new NBAData(); d.players = async () => roster; return d; };

test('nba compare is a reserved subcommand that needs players', () => {
  assert.deepEqual(parse('nba compare LeBron James vs Stephen Curry'), { command: 'compare', query: 'LeBron James vs Stephen Curry' });
  assert.throws(() => parse('nba compare'), /Name two players/);
});

test('two names split on vs, comma or pipe, or by trying every split point', async () => {
  const d = data();
  for (const q of ['LeBron James vs Stephen Curry', 'LeBron James, Stephen Curry', 'LeBron James | Stephen Curry', 'LeBron James Stephen Curry']) {
    assert.deepEqual((await d.comparePlayers(q)).map(s => s.player.id), ['lbj', 'sc'], q);
  }
});

test('a fuzzy side comes back with suggestions instead of failing the whole compare', async () => {
  const sides = await data().comparePlayers('Stephen Curry Victor Wembi');
  assert.equal(sides[0].player.id, 'sc');
  assert.equal(sides[1].suggestions[0].id, 'wemby');
  await assert.rejects(data().comparePlayers('Stephen Curry vs Zzzzzz'), /No current NBA player matches “Zzzzzz”/);
  await assert.rejects(data().comparePlayers('Curry'), /Name two players/);
  await assert.rejects(data().comparePlayers('a, b, c'), /two players at a time/);
});

test('higher wins, turnovers are lower-is-better, ties and games played are never highlighted', () => {
  assert.equal(leader(20, 10, 1), 0); assert.equal(leader(10, 20, 1), 1);
  assert.equal(leader(2, 3, -1), 0); assert.equal(leader(5, 5, 1), -1); assert.equal(leader(4, 2, 0), -1); assert.equal(leader(null, 2, 1), -1);
  const side = (id, stats, games, record) => ({ player: { id, displayName: id, team: { displayName: 'Team' } }, stats, games, record });
  const html = compareHTML({ label: '2026-27 Regular Season', sides: [
    side('A', { PTS: '30.0', TO: '4.0', REB: '5.0' }, 4, '4-1'), side('B', { PTS: '20.0', TO: '2.0', REB: '5.0' }, 4, '2-3'),
  ] });
  assert.match(html, /val best left[^>]*>30\.0</); assert.match(html, /val dim right[^>]*>20\.0</);
  assert.match(html, /val best right[^>]*>2\.0</);
  assert.match(html, /val  left[^>]*>5\.0</);
  assert.match(html, /record best"><b>4-1</);
});
