const { test } = require('node:test');
const assert = require('node:assert/strict');
const { parse } = require('../commands');
const { NBAData, appearances } = require('../data');
const { suggestGame } = require('../suggestions');
const now = new Date('2026-10-20T21:00:00Z');
function log(year, entries, years = [2027, 2026, 2025]) {
  return { filters: [{ name: 'season', value: String(year), options: years.map(y => ({ value: String(y) })) }], labels: ['MIN', 'PTS'],
    seasonTypes: [{ categories: [{ type: 'event', events: entries.map(e => ({ eventId: e.id, stats: [e.minutes ?? '20', e.points ?? '10'] })) }] }],
    events: Object.fromEntries(entries.map(e => [e.id, { gameDate: e.at, eventNote: e.note || '' }])) };
}
test('short dates resolve to the most recent occurrence and accept explicit years', () => {
  assert.equal(parse('nba Jalen Duren 26/Dec', now).date, '2025-12-26');
  assert.equal(parse('nba Jalen Duren 1/oct', now).date, '2026-10-01');
  assert.equal(parse('nba Jalen Duren 26/Dec/2024', now).date, '2024-12-26');
  assert.equal(parse('nba Jalen Duren', now).date, undefined);
  assert.throws(() => parse('nba Jalen Duren 31/Apr', now), /valid game date/);
  assert.throws(() => parse('nba Jalen Duren 26/XYZ', now), /month/);
  assert.throws(() => parse('nba Jalen Duren 29/Feb/2025', now), /valid game date/);
  assert.equal(parse('nba Jalen Duren 29/Feb/2024', now).date, '2024-02-29');
  assert.equal(parse('nba Jalen Duren 20/Oct', new Date('2026-10-20T01:00:00Z')).date, '2025-10-20');
});
test('appearance dates use NBA local dates and exclude DNPs and All-Star exhibitions', () => {
  const data = log(2026, [{ id: 'yes', at: '2025-12-27T01:00:00Z' }, { id: 'dnp', at: '2025-12-25T00:00:00Z', points: '--' }, { id: 'allstar', at: '2026-02-16T00:00:00Z', note: 'NBA All-Star' }]);
  assert.deepEqual(appearances(data), [{ eventId: 'yes', date: '2025-12-26' }]);
});
test('closest appearance crosses season boundaries and breaks ties toward the earlier game', async () => {
  const data = new NBAData(), loaded = [];
  data.league = async () => ({ season: { year: 2027 } });
  data.json = async url => {
    const year = Number(new URL(url).searchParams.get('season')); loaded.push(year);
    return log(year, year === 2027 ? [{ id: 'later', at: '2026-10-01T23:00Z' }] : year === 2026 ? [{ id: 'earlier', at: '2026-06-29T23:00Z' }] : []);
  };
  assert.equal((await data.closestAppearance({ id: 'p' }, '2026-07-01')).eventId, 'earlier');
  assert.ok(!loaded.includes(2025));
  data.json = async () => log(2026, [{ id: 'before', at: '2025-12-25T23:00Z' }, { id: 'after', at: '2025-12-27T23:00Z' }], [2026]);
  assert.equal((await data.closestAppearance({ id: 'p' }, '2025-12-26')).eventId, 'before');
});
test('dated lookup finds an appearance for a former team and offers alternatives for DNP', async () => {
  const data = new NBAData();
  data.json = async () => ({ events: [{ id: 'old-team', status: { type: { state: 'post' } } }] });
  data.game = async () => ({ played: true, eventId: 'old-team' });
  assert.equal((await data.datedGame({ id: 'p', team: { id: 'new-team' } }, '2025-12-26')).model.eventId, 'old-team');
  data.game = async () => ({ played: false });
  data.closestAppearance = async () => ({ eventId: 'near', date: '2025-12-25' });
  assert.equal((await data.datedGame({ id: 'p' }, '2025-12-26')).suggestion.eventId, 'near');
});
test('no-date lookup falls back to latest played game when the slate has no team game', async () => {
  const data = new NBAData();
  data.json = async () => ({ events: [] });
  data.latestGame = async player => ({ player, eventId: 'yesterday', played: true });
  assert.equal((await data.game({ id: 'p', team: { id: '20' } })).eventId, 'yesterday');
  await assert.rejects(data.game({ id: 'p', team: { id: '20' } }, '2026-10-21'), /no game on/);
});
test('latest game excludes future and DNP appearances and keeps its real date', async () => {
  const data = new NBAData();
  data.league = async () => ({ season: { year: 2027 } });
  data.json = async () => log(2027, [
    { id: 'future', at: '2026-10-22T23:00Z' },
    { id: 'dnp', at: '2026-10-21T01:00Z', points: '--' },
    { id: 'yesterday', at: '2026-10-20T23:00Z' },
    { id: 'older', at: '2026-10-18T23:00Z' },
  ], [2027]);
  data.game = async (player, date, eventId) => ({ played: true, eventId, date: '2026-10-20T23:00Z', note: '' });
  const model = await data.latestGame({ id: 'p' }, new Date('2026-10-21T19:00Z'));
  assert.equal(model.eventId, 'yesterday'); assert.equal(model.date, '2026-10-20T23:00Z');
  assert.match(model.note, /Latest played game/);
});
test('current live game remains preferred over latest-game fallback', async () => {
  const data = new NBAData();
  data.json = async url => url.includes('scoreboard')
    ? { events: [{ id: 'live', status: { type: { state: 'in' } }, competitions: [{ competitors: [{ team: { id: '20' } }] }] }] }
    : { header: { id: 'live', competitions: [{ status: { type: { state: 'in' } } }] } };
  data.latestGame = async () => { throw Error('Should not use fallback'); };
  assert.equal((await data.game({ id: 'p', team: { id: '20' } })).eventId, 'live');
});

test('default card keeps the last played game until tip-off, then switches to live', async () => {
  const data = new NBAData();
  const player = { id: 'p', team: { id: '20' } };
  let state = 'pre';
  const fetched = [];
  data.json = async url => {
    fetched.push(url);
    return url.includes('scoreboard')
      ? { events: [{ id: 'tonight', status: { type: { state } }, competitions: [{ competitors: [{ team: { id: '20' } }] }] }] }
      : { header: { id: 'tonight', competitions: [{ status: { type: { state } } }] } };
  };
  data.latestGame = async () => ({ eventId: 'last-played', state: 'post' });
  assert.equal((await data.game(player)).eventId, 'last-played');
  assert.equal(fetched.some(url => url.includes('summary')), false);
  state = 'in';
  assert.equal((await data.game(player)).eventId, 'tonight');
  state = 'post';
  assert.equal((await data.game(player)).eventId, 'tonight');
});

test('explicit dates, pinned events and watches can still load an upcoming game', async () => {
  const data = new NBAData();
  const player = { id: 'p', team: { id: '20' } };
  data.json = async url => url.includes('scoreboard')
    ? { events: [{ id: 'tonight', status: { type: { state: 'pre' } }, competitions: [{ competitors: [{ team: { id: '20' } }] }] }] }
    : { header: { id: 'tonight', competitions: [{ status: { type: { state: 'pre' } } }] } };
  data.latestGame = async () => { throw Error('Explicit lookup must not fall back'); };
  assert.equal((await data.game(player, '2026-11-02')).eventId, 'tonight');
  assert.equal((await data.game(player, null, 'tonight')).eventId, 'tonight');
  assert.equal((await data.game(player, null, undefined, { includeUpcoming: true })).eventId, 'tonight');
});
function confirmation(customId) {
  const edits = [], calls = [];
  return { edits, calls, options: {
    userId: 'owner', player: { id: 'p', displayName: 'Jalen Duren' }, date: '2025-12-26', suggestion: { date: '2025-12-25', eventId: 'nearest' },
    reply: { edit: async body => edits.push(body), awaitMessageComponent: async options => {
      assert.equal(options.filter({ user: { id: 'owner' } }), true);
      assert.equal(options.filter({ user: { id: 'other' }, reply: async () => {} }), false);
      if (customId === 'timeout') throw new Error('timeout');
      return { customId, deferUpdate: async () => calls.push('ack') };
    } },
    data: { game: async (player, date, id) => { calls.push(id); return { played: true }; } },
    renderer: { render: async () => { calls.push('render'); return Buffer.from('png'); } },
    payload: () => ({ content: 'box score', files: ['image'] }),
  } };
}
test('confirmation only fetches the alternative after the requester clicks Yes', async () => {
  const fixture = confirmation('nba-date-yes'); await suggestGame(fixture.options);
  assert.deepEqual(fixture.calls, ['ack', 'nearest', 'render']);
  assert.deepEqual(fixture.edits.at(-1).components, []);
});

test('confirmed alternative games use the same flip card presenter', async () => {
  const fixture = confirmation('nba-date-yes');
  let shown;
  fixture.options.showCard = async (message, model) => { shown = { message, model }; };
  await suggestGame(fixture.options);
  assert.equal(shown.message, fixture.options.reply);
  assert.equal(shown.model.played, true);
  assert.deepEqual(fixture.calls, ['ack', 'nearest']);
});
test('No and timeout remove buttons without loading an alternative', async () => {
  for (const choice of ['nba-date-no', 'timeout']) {
    const fixture = confirmation(choice); await suggestGame(fixture.options);
    assert.ok(!fixture.calls.includes('nearest'));
    assert.deepEqual(fixture.edits.at(-1).components, []);
  }
});
