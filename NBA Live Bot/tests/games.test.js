const { test } = require('node:test');
const assert = require('node:assert/strict');
const { parse } = require('../commands');
const { NBAData } = require('../data');
const { gamesPayload } = require('../games');
const { gamesHTML, gameStatus } = require('../games_layout');
function event(id, state, date, name = '') {
  return { id, date, status: { type: { state, name, shortDetail: state === 'in' ? '3:27 - 3rd' : 'Scheduled' } },
    competitions: [{ competitors: [
      { homeAway: 'home', team: { displayName: 'Home Team', abbreviation: 'HOM' }, score: '75' },
      { homeAway: 'away', team: { displayName: 'Away Team', abbreviation: 'AWY' }, score: '83' },
    ] }] };
}

test('nba games is a reserved case-insensitive command, not a player search', () => {
  assert.deepEqual(parse('NBA GAMES'), { command: 'games' });
  assert.deepEqual(parse('nba   games  '), { command: 'games' });
  assert.throws(() => parse('nba games tomorrow'), /Use !nba games/);
  assert.equal(parse('nba LeBron James').query, 'LeBron James');
});

test('slate uses only the scoreboard and sorts live games before upcoming games', async () => {
  const data = new NBAData();
  const urls = [];
  data.json = async url => {
    urls.push(url);
    return { day: { date: '2026-10-25' }, events: [
      event('late', 'pre', '2026-10-26T02:00Z'), event('final', 'post', '2026-10-25T19:00Z'),
      event('early', 'pre', '2026-10-26T01:00Z'), event('live', 'in', '2026-10-25T23:00Z'),
      event('canceled', 'pre', '2026-10-26T02:00Z', 'STATUS_CANCELED'),
      event('postponed', 'pre', '2026-10-26T02:00Z', 'STATUS_POSTPONED'),
      event('suspended', 'in', '2026-10-25T23:00Z', 'STATUS_SUSPENDED'),
    ] };
  };
  const slate = await data.games();
  assert.equal(urls.length, 1); assert.match(urls[0], /\/scoreboard$/);
  assert.equal(slate.date, '2026-10-25');
  assert.deepEqual(slate.games.map(g => g.id), ['live', 'early', 'late']);
  assert.equal(slate.games[0].away.score, '83');
});

test('scoreboard shows readable live scores and explicitly labelled tip-off times', async () => {
  const data = new NBAData();
  data.json = async () => ({ day: { date: '2026-10-25' }, events: [event('live', 'in', '2026-10-25T23:00Z'), event('next', 'pre', '2026-10-26T01:00Z')] });
  const markup = gamesHTML({ ...await data.games(), updatedAt: '2026-10-25T23:30Z' });
  assert.match(markup, /25 October 2026/);
  assert.match(markup, /LIVE NOW/); assert.match(markup, /COMING UP/);
  assert.match(markup, /<strong>83<\/strong>/); assert.match(markup, /Q3 · 3:27/);
  assert.match(markup, /01:00 GMT/); assert.match(markup, /Europe\/Dublin/);
  assert.doesNotMatch(markup, /minutes ago/);
});

test('empty and finished slates explain that there are no live/upcoming games', async () => {
  for (const events of [[], [event('done', 'post', '2026-10-25T23:00Z')]]) {
    const data = new NBAData(); data.json = async () => ({ events });
    const markup = gamesHTML(await data.games());
    assert.match(markup, /No live or upcoming games remain/);
    assert.doesNotMatch(markup, /<article /);
  }
});

test('provider errors are not presented as no games scheduled', async () => {
  const data = new NBAData(); data.json = async () => ({});
  await assert.rejects(data.games(), /scoreboard is unavailable/);
});

test('every game is retained and missing tip-off times do not break the message', () => {
  const game = { away: { name: 'Away' }, home: { name: 'Home' }, state: 'pre', status: 'Scheduled' };
  const slate = { date: '2026-10-25', games: Array.from({ length: 21 }, () => game) };
  const markup = gamesHTML(slate);
  assert.equal((markup.match(/<article /g) || []).length, 21);
  assert.match(markup, /Time TBC/);
  const png = Buffer.from('full-resolution-png');
  const body = gamesPayload(slate, png);
  assert.equal(body.files[0].attachment, png);
  assert.equal(body.files[0].name, 'nba-games.png');
  assert.equal(body.embeds, undefined);
});

test('late pregame feed is awaiting tip-off, never incorrectly marked live', () => {
  const game = { state: 'pre', date: '2026-10-27T00:00Z', status: 'Scheduled' };
  assert.equal(gameStatus(game, '2026-10-27T00:02Z', 'Europe/Dublin').badge, 'AWAITING TIP-OFF');
  assert.equal(gameStatus({ ...game, status: 'Delayed' }, '2026-10-27T00:02Z', 'Europe/Dublin').badge, 'DELAYED');
});

test('image content is escaped and only trusted team logo URLs are embedded', () => {
  const game = { state: 'in', status: '<script>', away: { name: '<unsafe>', logo: 'https://evil.test/x' }, home: { name: 'Home' } };
  const markup = gamesHTML({ games: [game] });
  assert.match(markup, /&lt;unsafe&gt;/); assert.doesNotMatch(markup, /<script>/);
  assert.doesNotMatch(markup, /https:\/\/evil/);
});

test('a finished slate rolls forward to the next night instead of showing an empty board', async () => {
  const data = new NBAData();
  const urls = [];
  data.json = async url => {
    urls.push(url);
    if (url.endsWith('/scoreboard')) return { day: { date: '2026-10-28' }, events: [event('done', 'post', '2026-10-28T23:00Z')] };
    return { events: [event('tonight', 'pre', '2026-10-29T23:00Z')] };
  };
  const slate = await data.games();
  assert.match(urls[1], /dates=20261029$/);
  assert.equal(slate.date, '2026-10-29');
  assert.deepEqual(slate.games.map(g => g.id), ['tonight']);
  // Box scores keep last night's finished games.
  assert.deepEqual((await data.games({ all: true })).games.map(g => g.id), ['done']);
});
