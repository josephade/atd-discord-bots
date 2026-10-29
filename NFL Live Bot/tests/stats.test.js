const { test } = require('node:test');
const assert = require('node:assert/strict');
const { derived, extractGame, extractBoxscore, seasonStats, NFLData } = require('../data');
const { parse } = require('../commands');
test('ESPN standard PPR fantasy scoring by position', () => {
  const qb = derived({ passingYards: '275', passingTouchdowns: '2', interceptions: '1', rushingYards: '18', rushingTouchdowns: '0' });
  assert.equal(qb.FP, (0.04 * 275 + 4 * 2 - 2 * 1 + 0.1 * 18).toFixed(1));
  const rb = derived({ rushingYards: '100', rushingTouchdowns: '1', receptions: '3', receivingYards: '20', receivingTouchdowns: '0', fumblesLost: '1' });
  assert.equal(rb.FP, (0.1 * 100 + 6 * 1 + 3 + 0.1 * 20 - 2 * 1).toFixed(1));
  const kicker = derived({ 'fieldGoalsMade/fieldGoalAttempts': '2/3', 'extraPointsMade/extraPointAttempts': '3/3' });
  assert.equal(kicker.FP, (3 * 2 - 1 + 1 * 3).toFixed(1));
  // Made-FG distances (from scoring plays) score 3 under 40, 4 for 40-49, 5 for 50+.
  const byDistance = derived({ 'fieldGoalsMade/fieldGoalAttempts': '3/4', fieldGoalDistances: [22, 44, 53], 'extraPointsMade/extraPointAttempts': '2/2' });
  assert.equal(byDistance.FP, (3 + 4 + 5 - 1 + 2).toFixed(1));
  // Gamelog rows carry distance buckets instead.
  const buckets = derived({ 'fieldGoalsMade20_29-fieldGoalAttempts20_29': '1-1', 'fieldGoalsMade40_49-fieldGoalAttempts40_49': '1-1', 'fieldGoalsMade50-fieldGoalAttempts50': '0-1', 'fieldGoalsMade-fieldGoalAttempts': '2-3', 'extraPointsMade-extraPointAttempts': '5-5' });
  assert.equal(buckets.FP, (3 + 4 - 1 + 5).toFixed(1));
  const kickerGamelogSpelling = derived({ 'fieldGoalsMade-fieldGoalAttempts': '1-1', 'extraPointsMade-extraPointAttempts': '1-1' });
  assert.equal(kickerGamelogSpelling.FP, '4.0');
  // No offensive or kicking stats: no universal "default" IDP formula exists, so no FP tile.
  const defender = derived({ totalTackles: '7', sacks: '1', passesDefended: '1' });
  assert.equal(defender.FP, '—');
  assert.equal(derived({}).FP, '—');
});
test('box score blocks are kept separate by category instead of merged into one flat row', () => {
  const summary = {
    header: { id: '1', season: { year: 2026 }, competitions: [{ date: '2025-10-22T23:00Z', status: { type: { state: 'post', shortDetail: 'Final' } }, competitors: [{ team: { abbreviation: 'DAL' }, homeAway: 'home', score: '20' }, { team: { abbreviation: 'NE' }, homeAway: 'away', score: '17' }] }] },
    boxscore: { players: [{ team: { abbreviation: 'DAL' }, statistics: [
      { name: 'passing', keys: ['completions/passingAttempts', 'passingYards', 'passingTouchdowns', 'interceptions'], labels: ['C/ATT', 'YDS', 'TD', 'INT'], athletes: [{ athlete: { id: '9', displayName: 'Test QB' }, stats: ['20/30', '275', '2', '1'], didNotPlay: false }] },
      { name: 'rushing', keys: ['rushingAttempts', 'rushingYards', 'rushingTouchdowns', 'longRushing'], labels: ['CAR', 'YDS', 'TD', 'LONG'], athletes: [{ athlete: { id: '9', displayName: 'Test QB' }, stats: ['4', '18', '0', '9'], didNotPlay: false }] },
    ] }] },
  };
  const game = extractGame(summary, { id: '9' });
  assert.equal(game.primary, 'passing');
  assert.equal(game.categories.length, 2);
  assert.equal(game.categories[0].key, 'passing'); assert.equal(game.categories[1].key, 'rushing');
  // A QB's rushing YDS must not clobber passing YDS when flattened for the fantasy formula.
  assert.equal(game.stats.passingYards, '275'); assert.equal(game.stats.rushingYards, '18');
  assert.equal(game.state, 'post'); assert.equal(game.matchup, 'NE 17  ·  DAL 20');
  summary.boxscore.players[0].statistics[0].athletes[0].didNotPlay = true;
  assert.match(extractGame(summary, { id: '9' }).note, /Inactive/);
  assert.match(extractGame(summary, { id: 'missing' }).note, /No player box score/);
});
test('box score extraction keeps one table per non-empty category per team', () => {
  const summary = {
    header: { id: '1', competitions: [{ status: { type: { state: 'post', shortDetail: 'Final' } }, competitors: [{ team: { id: '1', abbreviation: 'DAL', displayName: 'Dallas' }, homeAway: 'home', score: '20' }, { team: { id: '2', abbreviation: 'NE', displayName: 'New England' }, homeAway: 'away', score: '17' }] }] },
    boxscore: { players: [{ team: { id: '1' }, statistics: [
      { name: 'passing', keys: ['passingYards'], labels: ['YDS'], totals: ['275'], athletes: [{ athlete: { id: '9', displayName: 'Test QB' }, stats: ['275'], starter: true }] },
    ] }, { team: { id: '2' }, statistics: [] }] },
  };
  const box = extractBoxscore(summary);
  assert.equal(box.teams[0].abbreviation, 'NE'); assert.equal(box.teams[1].abbreviation, 'DAL');
  assert.equal(box.teams[1].categories.length, 1);
  assert.equal(box.teams[1].categories[0].players[0].name, 'Test QB');
  assert.equal(box.teams[0].categories.length, 0);
});
test('season averages recompute rates from totals and use the season’s starting year', () => {
  const e1 = { eventId: '1', stats: ['20', '30', '275', '66.7', '9.2', '2', '1', '42', '3-19', '98.0', '55.0'] };
  const e2 = { eventId: '2', stats: ['10', '20', '150', '50.0', '7.5', '0', '2', '30', '1-6', '70.0', '40.0'] };
  const log = { filters: [{ name: 'season', value: '2025' }], names: ['completions', 'passingAttempts', 'passingYards', 'completionPct', 'yardsPerPassAttempt', 'passingTouchdowns', 'interceptions', 'longPassing', 'sacks-sackYardsLost', 'QBRating', 'adjQBR'],
    seasonTypes: [
      { displayName: '2025 Regular Season', categories: [{ type: 'event', events: [e1, e2, e1] }] },
      { displayName: '2025 Postseason', categories: [{ type: 'event', events: [e1] }] },
    ] };
  const season = seasonStats(log, 2025);
  assert.equal(season.games, 2);
  assert.equal(season.stats.passingYards, '212.5');
  // completionPct recomputed from summed completions/attempts (30/50), not averaged per game ((66.7+50)/2).
  assert.equal(season.stats.completionPct, '60.0');
  assert.equal(season.stats['completions/passingAttempts'], '30/50');
  assert.equal(season.stats.longPassing, '42');
  assert.equal(seasonStats(log, 2025, true).games, 1);
  assert.throws(() => seasonStats(log, 2026), /requested season/);
  log.seasonTypes = []; assert.equal(seasonStats(log, 2025).games, 0);
});
test('season stats exclude Pro Bowl appearances and deduplicate events', () => {
  const e1 = { eventId: '1', stats: ['100'] };
  const log = { filters: [{ name: 'season', value: '2025' }], names: ['rushingYards'],
    seasonTypes: [{ displayName: '2025 Regular Season', categories: [{ type: 'event', events: [e1, e1] }] }],
    events: { '1': { eventNote: 'NFC Pro Bowl' } } };
  assert.equal(seasonStats(log, 2025).games, 0);
});
test('commands preserve names, validate dates, and use the season’s starting year', () => {
  assert.deepEqual(parse('nfl Justin Jefferson 2025-10-22'), { command: 'nfl', query: 'Justin Jefferson', date: '2025-10-22', year: undefined, playoffs: false });
  assert.equal(parse('season Patrick Mahomes 2025-26 playoffs').year, 2025);
  assert.equal(parse('season Patrick Mahomes 2025').year, 2025);
  assert.throws(() => parse('nfl Justin Jefferson 2025-02-30'), /valid game date/);
  assert.throws(() => parse('season Patrick Mahomes 2025-27'), /season/);
  assert.throws(() => parse('nfl'), /player name/);
  assert.equal(parse('unrelated command'), null);
});
test('ambiguous names do not silently choose a player', async () => {
  const data = new NFLData(); data.players = async () => [{ id: '1', displayName: 'Jaylen Williams' }, { id: '2', displayName: 'Jalen Williams' }];
  await assert.rejects(data.findPlayer('Williams'), /More than one match/);
  assert.equal((await data.findPlayer('Jalen Williams')).id, '2');
});
test('watch refresh keeps the selected event instead of following next week’s slate', async () => {
  const data = new NFLData(); let requested;
  data.json = async url => { requested = url; return { header: { id: '123', competitions: [{ status: { type: { state: 'in' } } }] } }; };
  await data.game({ id: '1' }, null, '123');
  assert.match(requested, /summary\?event=123$/);
});
