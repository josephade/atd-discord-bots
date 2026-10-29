const { test } = require('node:test');
const assert = require('node:assert/strict');
const { parse } = require('../commands');
const { NFLData, flattenCalendar } = require('../data');
const { gamesPayload } = require('../games');
const { gamesHTML, gameStatus } = require('../games_layout');
function event(id, state, date, name = '') {
  return { id, date, status: { type: { state, name, shortDetail: state === 'in' ? '3:27 - 3rd' : 'Scheduled' } },
    competitions: [{ competitors: [
      { homeAway: 'home', team: { displayName: 'Home Team', abbreviation: 'HOM' }, score: '17' },
      { homeAway: 'away', team: { displayName: 'Away Team', abbreviation: 'AWY' }, score: '20' },
    ] }] };
}
// Mirrors the shape of ESPN's real scoreboard "calendar": three season-type groups,
// each with its own ordered week entries, plus an entry-less "Off Season" group.
const calendar = [
  { value: '1', entries: [{ value: '1', label: 'Preseason Week 1', detail: 'Aug 13-19' }] },
  { value: '2', entries: [
    { value: '7', label: 'Week 7', detail: 'Oct 21-27', startDate: '2026-10-21T07:00Z', endDate: '2026-10-28T06:59Z' },
    { value: '8', label: 'Week 8', detail: 'Oct 28-Nov 3', startDate: '2026-10-28T07:00Z', endDate: '2026-11-04T07:59Z' },
    { value: '9', label: 'Week 9', detail: 'Nov 4-10', startDate: '2026-11-04T08:00Z', endDate: '2026-11-11T07:59Z' },
  ] },
  { value: '3', entries: [{ value: '1', label: 'Wild Card', detail: 'Jan 13-19' }] },
  { value: '4' },
];
function week(number, events) {
  return { week: { number }, leagues: [{ calendar, season: { year: 2026, type: { type: 2 } } }], events };
}

test('nfl games is a reserved case-insensitive command, not a player search', () => {
  assert.deepEqual(parse('NFL GAMES'), { command: 'games' });
  assert.deepEqual(parse('nfl   games  '), { command: 'games' });
  assert.throws(() => parse('nfl games tomorrow'), /Use !nfl games/);
  assert.equal(parse('nfl Patrick Mahomes').query, 'Patrick Mahomes');
});

test('flattenCalendar orders every season-type group and skips the entry-less off-season group', () => {
  const entries = flattenCalendar(calendar);
  assert.equal(entries.length, 5);
  assert.deepEqual(entries.map(e => e.label), ['Preseason Week 1', 'Week 7', 'Week 8', 'Week 9', 'Wild Card']);
  assert.deepEqual(entries.map(e => e.seasonType), [1, 2, 2, 2, 3]);
});

test('slate browses by week, not by calendar day, and sorts live games before upcoming games', async () => {
  const data = new NFLData();
  const urls = [];
  data.json = async url => {
    urls.push(url);
    return week(8, [
      event('late', 'pre', '2026-11-02T18:00Z'), event('final', 'post', '2026-10-29T19:00Z'),
      event('early', 'pre', '2026-11-02T13:00Z'), event('live', 'in', '2026-11-01T23:00Z'),
      event('canceled', 'pre', '2026-11-02T18:00Z', 'STATUS_CANCELED'),
      event('postponed', 'pre', '2026-11-02T18:00Z', 'STATUS_POSTPONED'),
      event('suspended', 'in', '2026-11-01T23:00Z', 'STATUS_SUSPENDED'),
    ]);
  };
  const slate = await data.games();
  assert.equal(urls.length, 1); assert.match(urls[0], /\/scoreboard$/);
  assert.equal(slate.week, 8); assert.equal(slate.seasonType, 2); assert.equal(slate.year, 2026);
  assert.equal(slate.label, 'Week 8'); assert.equal(slate.detail, 'Oct 28-Nov 3');
  assert.equal(slate.calendar.length, 5);
  assert.deepEqual(slate.games.map(g => g.id), ['live', 'early', 'late']);
  assert.equal(slate.games[0].away.score, '20');
});

test('an explicit date resolves to the NFL week that contains it, not that single day', async () => {
  const data = new NFLData();
  const urls = [];
  data.json = async url => { urls.push(url); return week(9, [event('sun', 'post', '2026-11-08T18:00Z')]); };
  const slate = await data.games({ date: '2026-11-08' });
  assert.equal(urls.length, 2);
  assert.match(urls[0], /dates=20261108/);
  assert.match(urls[1], /week=9&seasontype=2&dates=2026/);
  assert.equal(slate.week, 9);
});

test('a date with zero games (most days, since the league plays Thu/Sun/Mon) still resolves a week via the calendar date ranges', async () => {
  const data = new NFLData();
  const urls = [];
  data.json = async url => {
    urls.push(url);
    if (url.includes('week=')) return week(7, [event('thu', 'post', '2026-10-22T23:00Z')]);
    // A Tuesday: ESPN returns zero events and omits "week" entirely for this exact date.
    return { leagues: [{ calendar, season: { year: 2026, type: { type: 2 } } }], events: [] };
  };
  const slate = await data.games({ all: true, date: '2026-10-27' });
  assert.equal(urls.length, 2);
  assert.match(urls[1], /week=7&seasontype=2&dates=2026/);
  assert.equal(slate.week, 7); assert.equal(slate.games.length, 1);
});

test('scoreboard shows readable live scores and explicitly labelled kickoff times', async () => {
  const data = new NFLData();
  data.json = async () => week(8, [event('live', 'in', '2026-11-01T23:00Z'), event('next', 'pre', '2026-11-02T01:00Z')]);
  const markup = gamesHTML({ ...await data.games(), updatedAt: '2026-11-01T23:30Z' });
  assert.match(markup, /Week 8/); assert.match(markup, /Oct 28-Nov 3/);
  assert.match(markup, /LIVE NOW/); assert.match(markup, /COMING UP/);
  assert.match(markup, /<strong>20<\/strong>/); assert.match(markup, /Q3 · 3:27/);
  assert.match(markup, /01:00 GMT/); assert.match(markup, /Europe\/Dublin/);
  assert.doesNotMatch(markup, /minutes ago/);
});

test('empty and finished slates explain that there are no live/upcoming games', async () => {
  for (const events of [[], [event('done', 'post', '2026-10-25T23:00Z')]]) {
    const data = new NFLData(); data.json = async () => ({ events });
    const markup = gamesHTML(await data.games());
    assert.match(markup, /No live or upcoming games remain/);
    assert.doesNotMatch(markup, /<article /);
  }
});

test('provider errors are not presented as no games scheduled', async () => {
  const data = new NFLData(); data.json = async () => ({});
  await assert.rejects(data.games(), /scoreboard is unavailable/);
});

test('every game is retained and missing kickoff times do not break the message', () => {
  const game = { away: { name: 'Away' }, home: { name: 'Home' }, state: 'pre', status: 'Scheduled' };
  const slate = { label: 'Week 8', games: Array.from({ length: 16 }, () => game) };
  const markup = gamesHTML(slate);
  assert.equal((markup.match(/<article /g) || []).length, 16);
  assert.match(markup, /Time TBC/);
  const png = Buffer.from('full-resolution-png');
  const body = gamesPayload(slate, png);
  assert.equal(body.files[0].attachment, png);
  assert.equal(body.files[0].name, 'nfl-games.png');
  assert.equal(body.embeds, undefined);
});

test('late pregame feed is awaiting kickoff, never incorrectly marked live', () => {
  const game = { state: 'pre', date: '2026-10-27T00:00Z', status: 'Scheduled' };
  assert.equal(gameStatus(game, '2026-10-27T00:02Z', 'Europe/Dublin').badge, 'AWAITING KICKOFF');
  assert.equal(gameStatus({ ...game, status: 'Delayed' }, '2026-10-27T00:02Z', 'Europe/Dublin').badge, 'DELAYED');
});

test('image content is escaped and only trusted team logo URLs are embedded', () => {
  const game = { state: 'in', status: '<script>', away: { name: '<unsafe>', logo: 'https://evil.test/x' }, home: { name: 'Home' } };
  const markup = gamesHTML({ games: [game] });
  assert.match(markup, /&lt;unsafe&gt;/); assert.doesNotMatch(markup, /<script>/);
  assert.doesNotMatch(markup, /https:\/\/evil/);
});
