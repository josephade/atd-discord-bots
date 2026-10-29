const { test } = require('node:test');
const assert = require('node:assert/strict');
const { parse } = require('../commands');
const { NBAData } = require('../data');
const { last10HTML } = require('../last10_layout');

const labels = ['MIN', 'FG', 'FG%', '3PT', '3P%', 'FT', 'FT%', 'REB', 'AST', 'BLK', 'STL', 'PF', 'TO', 'PTS'];
const line = pts => ['30', '5-10', '50.0', '1-2', '50.0', '2-2', '100.0', '5', '4', '1', '1', '2', '2', String(pts)];
function log(year, games, extra = {}) {
  const events = {}, regular = [], post = [], pre = [];
  for (const g of games) {
    events[g.id] = { gameDate: g.date, atVs: g.home ? 'vs' : '@', homeTeamScore: g.home ? '110' : '100', awayTeamScore: g.home ? '100' : '110',
      gameResult: 'W', opponent: { abbreviation: 'OPP' }, eventNote: g.note || '' };
    (g.type === 'post' ? post : g.type === 'pre' ? pre : regular).push({ eventId: g.id, stats: g.dnp ? [] : line(g.pts ?? 20) });
  }
  const type = (name, list) => ({ displayName: `${year - 1}-${String(year).slice(2)} ${name}`, categories: [{ type: 'event', events: list }] });
  return { filters: [{ name: 'season', value: String(year) }], labels, events,
    seasonTypes: [type('Postseason', post), type('Regular Season', regular), type('Preseason', pre)], ...extra };
}

test('recent is its own command and needs a player', () => {
  assert.deepEqual(parse('recent LeBron James'), { command: 'recent', query: 'LeBron James' });
  assert.deepEqual(parse('RECENT   Jalen Duren'), { command: 'recent', query: 'Jalen Duren' });
  assert.throws(() => parse('recent'), /Include a player name/);
  assert.equal(parse('last10 LeBron James'), null);
});

test('recent games stay in the current season, newest first, skipping DNPs, preseason and All-Star', async () => {
  const data = new NBAData();
  data.league = async () => ({ season: { year: 2027 } });
  const urls = [];
  const games = Array.from({ length: 12 }, (_, i) => ({ id: `g${i}`, date: `2027-0${i < 2 ? 5 : 3}-${String(28 - i).padStart(2, '0')}T23:00Z`, type: i < 2 ? 'post' : 'reg', home: i === 1, pts: i === 1 ? 30 : 20 }));
  const current = log(2027, games.concat([{ id: 'dnp', date: '2027-05-30T23:00Z', dnp: true }, { id: 'pre', date: '2026-10-10T23:00Z', type: 'pre' }, { id: 'asg', date: '2027-02-15T23:00Z', note: 'NBA All-Star Game' }]));
  data.json = async url => { urls.push(url); return current; };
  const result = await data.lastGames({ id: '1', displayName: 'Test Player' });
  assert.ok(urls.every(u => u.includes('season=2027')));
  assert.equal(result.games.length, 10);
  assert.deepEqual(result.games.slice(0, 2).map(g => g.id), ['g0', 'g1']);
  assert.ok(!result.games.some(g => ['dnp', 'pre', 'asg'].includes(g.id)));
  assert.equal(result.games[0].score, '110-100'); assert.equal(result.games[0].home, false); assert.equal(result.games[0].playoffs, true);
  assert.equal(result.averages['FG%'], '50.0');
  assert.equal(result.averages.PTS, ((30 + 20 * 9) / 10).toFixed(1));
  const html = last10HTML(result);
  assert.match(html, /LAST 10 GAMES/); assert.match(html, /5\/10/); assert.match(html, /class="best">30</);
});

test('early in the season it shows only the games played so far, never last season', async () => {
  const data = new NBAData();
  data.league = async () => ({ season: { year: 2027 } });
  data.json = async url => (url.includes('season=2027')
    ? log(2027, [{ id: 'c1', date: '2026-10-28T23:00Z' }, { id: 'c2', date: '2026-10-25T23:00Z' }])
    : log(2026, [{ id: 'old', date: '2026-04-10T23:00Z' }]));
  const result = await data.lastGames({ id: '1', displayName: 'Test Player' });
  assert.deepEqual(result.games.map(g => g.id), ['c1', 'c2']);
  assert.match(last10HTML(result), /LAST 2 GAMES/);
});

test('a player with no recorded games gets a clear error', async () => {
  const data = new NBAData();
  data.league = async () => ({ season: { year: 2027 } });
  data.json = async url => log(Number(url.match(/season=(\d+)/)[1]), []);
  await assert.rejects(data.lastGames({ id: '1', displayName: 'Rookie' }), /Rookie has not played a game this season yet/);
});
