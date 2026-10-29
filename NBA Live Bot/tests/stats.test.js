const { test } = require('node:test');
const assert = require('node:assert/strict');
const { derived, extractGame, seasonStats, NBAData } = require('../data');
const { parse } = require('../commands');
test('reference shooting and ESPN default fantasy scoring', () => {
  const s = derived({ PTS: '31', REB: '3', AST: '1', STL: '4', BLK: '0', TO: '3', FG: '10-14', FT: '5-5', '3PT': '6-8' });
  assert.equal(s['FG%'], '71.4'); assert.equal(s['TS%'], '95.7'); assert.equal(s.FP, '58.0');
  assert.equal(derived({})['FG%'], '—'); assert.equal(derived({}).FP, '—');
  assert.equal(derived({ PTS: 0, FG: '0-0', FT: '0-0' })['TS%'], '—');
  const zero = { PTS: 0, REB: 0, AST: 0, STL: 0, BLK: 0, TO: 0, FG: '0-0', FT: '0-0', '3PT': '0-0' };
  assert.equal(derived(zero).FP, '0.0');
  assert.equal(derived({ ...zero, PTS: 3, FG: '1-1', '3PT': '1-1' }).FP, '5.0');
  assert.equal(derived({ ...zero, FG: '0-2', FT: '0-2', TO: 1 }).FP, '-6.0');
  assert.equal(derived({ ...zero, '3PT': undefined }).FP, '—');
});
test('game parses provider labels regardless of order and respects DNP', () => {
  const summary = { header: { id: '1', season: { year: 2026 }, competitions: [{ date: '2025-10-22T23:00Z', status: { type: { state: 'post', shortDetail: 'Final' } }, competitors: [{ team: { abbreviation: 'BOS' }, homeAway: 'home', score: '117' }, { team: { abbreviation: 'PHI' }, homeAway: 'away', score: '118' }] }] }, boxscore: { players: [{ team: { abbreviation: 'PHI' }, statistics: [{ labels: ['FG', 'PTS'], athletes: [{ athlete: { id: '9', displayName: 'Test Player' }, stats: ['10-14', '31'], didNotPlay: false }] }] }] } };
  const game = extractGame(summary, { id: '9' });
  assert.equal(game.stats.PTS, '31'); assert.equal(game.stats['FG%'], '71.4'); assert.equal(game.state, 'post');
  assert.equal(game.matchup, 'PHI 118  ·  BOS 117');
  summary.boxscore.players[0].statistics[0].athletes[0].didNotPlay = true;
  assert.match(extractGame(summary, { id: '9' }).note, /DNP/);
  assert.equal(extractGame(summary, { id: '9' }).stats.FP, '—');
  assert.match(extractGame(summary, { id: 'missing' }).note, /No player box score/);
});
test('season uses weighted shooting, excludes playoffs and deduplicates games', () => {
  const e1 = { eventId: '1', stats: ['20', '1-2', '0-1', '0-0', '2', '2', '1', '0', '0', '0', '1'] };
  const e2 = { eventId: '2', stats: ['30', '9-10', '1-1', '2-2', '21', '4', '3', '1', '2', '1', '2'] };
  const log = { filters: [{ name: 'season', value: '2026' }], labels: ['MIN', 'FG', '3PT', 'FT', 'PTS', 'REB', 'AST', 'BLK', 'STL', 'PF', 'TO'], seasonTypes: [
    { displayName: '2025-26 Regular Season', categories: [{ type: 'event', events: [e1, e2, e1] }] },
    { displayName: '2025-26 Postseason', categories: [{ type: 'event', events: [e1] }] },
  ] };
  const season = seasonStats(log, 2026);
  assert.equal(season.games, 2); assert.equal(season.stats.PTS, '11.5'); assert.equal(season.stats['FG%'], '83.3');
  assert.equal(season.stats.FG, '5.0/6.0'); assert.equal(seasonStats(log, 2026, true).games, 1);
  assert.equal(season.stats.FP, '26.0'); // Individual game scores: 4 and 48.
  log.events = { '1': { eventNote: 'NBA All-Star - Championship' }, '2': { eventNote: 'NBA Cup - Quarterfinals' } };
  assert.equal(seasonStats(log, 2026).games, 1);
  log.events['2'].eventNote = 'NBA Cup - Final';
  assert.equal(seasonStats(log, 2026).games, 0);
  assert.throws(() => seasonStats(log, 2027), /requested season/);
  log.seasonTypes = []; assert.equal(seasonStats(log, 2026).games, 0);
});
test('commands preserve names, validate dates, and use season-ending years', () => {
  assert.deepEqual(parse('nba Paul George 2025-10-22'), { command: 'nba', query: 'Paul George', date: '2025-10-22', year: undefined, playoffs: false });
  assert.equal(parse('season LeBron James 2025-26 playoffs').year, 2026);
  assert.equal(parse('season LeBron James 2026').year, 2026);
  assert.throws(() => parse('nba Paul George 2025-02-30'), /valid game date/);
  assert.throws(() => parse('season LeBron James 2025-27'), /season/);
  assert.throws(() => parse('nba'), /player name/);
  assert.equal(parse('unrelated command'), null);
});
test('ambiguous names do not silently choose a player', async () => {
  const data = new NBAData(); data.players = async () => [{ id: '1', displayName: 'Jaylen Williams' }, { id: '2', displayName: 'Jalen Williams' }];
  await assert.rejects(data.findPlayer('Williams'), /More than one match/);
  assert.equal((await data.findPlayer('Jalen Williams')).id, '2');
});
test('watch refresh keeps the selected event instead of following tomorrow’s slate', async () => {
  const data = new NBAData(); let requested;
  data.json = async url => { requested = url; return { header: { id: '123', competitions: [{ status: { type: { state: 'in' } } }] } }; };
  await data.game({ id: '1' }, null, '123');
  assert.match(requested, /summary\?event=123$/);
});
