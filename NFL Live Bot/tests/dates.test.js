const { test } = require('node:test');
const assert = require('node:assert/strict');
const { parse } = require('../commands');
const { NFLData, appearances } = require('../data');
const { suggestGame } = require('../suggestions');
const now = new Date('2026-10-20T21:00:00Z');
function log(year, entries, years = [2027, 2026, 2025]) {
  return { filters: [{ name: 'season', value: String(year), options: years.map(y => ({ value: String(y) })) }], names: ['receivingYards'],
    seasonTypes: [{ categories: [{ type: 'event', events: entries.map(e => ({ eventId: e.id, stats: [e.yards ?? '80'] })) }] }],
    events: Object.fromEntries(entries.map(e => [e.id, { gameDate: e.at, eventNote: e.note || '' }])) };
}
test('short dates resolve to the most recent occurrence and accept explicit years', () => {
  assert.equal(parse('nfl Justin Jefferson 26/Dec', now).date, '2025-12-26');
  assert.equal(parse('nfl Justin Jefferson 1/oct', now).date, '2026-10-01');
  assert.equal(parse('nfl Justin Jefferson 26/Dec/2024', now).date, '2024-12-26');
  assert.equal(parse('nfl Justin Jefferson', now).date, undefined);
  assert.throws(() => parse('nfl Justin Jefferson 31/Apr', now), /valid game date/);
  assert.throws(() => parse('nfl Justin Jefferson 26/XYZ', now), /month/);
  assert.throws(() => parse('nfl Justin Jefferson 29/Feb/2025', now), /valid game date/);
  assert.equal(parse('nfl Justin Jefferson 29/Feb/2024', now).date, '2024-02-29');
  assert.equal(parse('nfl Justin Jefferson 20/Oct', new Date('2026-10-20T01:00:00Z')).date, '2025-10-20');
});
test('season tokens use the starting year, not the ending year', () => {
  assert.equal(parse('season Justin Jefferson 2025-26').year, 2025);
  assert.equal(parse('season Justin Jefferson 2025').year, 2025);
  assert.throws(() => parse('season Justin Jefferson 2025-27'), /season/);
});
test('appearance dates use the league local date and exclude DNPs and Pro Bowl exhibitions', () => {
  const data = log(2025, [{ id: 'yes', at: '2025-12-27T01:00:00Z' }, { id: 'dnp', at: '2025-12-25T00:00:00Z', yards: '--' }, { id: 'probowl', at: '2026-02-08T00:00:00Z', note: 'AFC Pro Bowl' }]);
  assert.deepEqual(appearances(data), [{ eventId: 'yes', date: '2025-12-26' }]);
});
test('closest appearance prioritizes seasons by distance and skips seasons far out of range', async () => {
  const data = new NFLData(), loaded = [];
  data.league = async () => ({ season: { year: 2026 } });
  data.json = async url => {
    const year = Number(new URL(url).searchParams.get('season')); loaded.push(year);
    return log(year, year === 2026 ? [{ id: 'later', at: '2026-09-01T23:00Z' }] : year === 2025 ? [{ id: 'earlier', at: '2026-01-15T23:00Z' }] : []);
  };
  assert.equal((await data.closestAppearance({ id: 'p' }, '2026-05-01')).eventId, 'earlier');
  assert.ok(!loaded.includes(2027));
});
test('dated lookup finds an appearance for a former team and offers alternatives for DNP', async () => {
  const data = new NFLData();
  data.json = async () => ({ events: [{ id: 'old-team', status: { type: { state: 'post' } } }] });
  data.game = async () => ({ played: true, eventId: 'old-team' });
  assert.equal((await data.datedGame({ id: 'p', team: { id: 'new-team' } }, '2025-12-26')).model.eventId, 'old-team');
  data.game = async () => ({ played: false });
  data.closestAppearance = async () => ({ eventId: 'near', date: '2025-12-25' });
  assert.equal((await data.datedGame({ id: 'p' }, '2025-12-26')).suggestion.eventId, 'near');
});
test('no-date lookup falls back to latest played game when the slate has no team game', async () => {
  const data = new NFLData();
  data.json = async () => ({ events: [] });
  data.latestGame = async player => ({ player, eventId: 'yesterday', played: true });
  assert.equal((await data.game({ id: 'p', team: { id: '20' } })).eventId, 'yesterday');
  await assert.rejects(data.game({ id: 'p', team: { id: '20' } }, '2026-10-21'), /no game on/);
});
test('latest game excludes future and DNP appearances and keeps its real date', async () => {
  const data = new NFLData();
  data.league = async () => ({ season: { year: 2026 } });
  data.json = async () => log(2026, [
    { id: 'future', at: '2026-10-22T23:00Z' },
    { id: 'dnp', at: '2026-10-21T01:00Z', yards: '--' },
    { id: 'yesterday', at: '2026-10-20T23:00Z' },
    { id: 'older', at: '2026-10-18T23:00Z' },
  ], [2026]);
  data.game = async (player, date, eventId) => ({ played: true, eventId, date: '2026-10-20T23:00Z', note: '' });
  const model = await data.latestGame({ id: 'p' }, new Date('2026-10-21T19:00Z'));
  assert.equal(model.eventId, 'yesterday'); assert.equal(model.date, '2026-10-20T23:00Z');
  assert.match(model.note, /Latest played game/);
});
test('current live game remains preferred over latest-game fallback', async () => {
  const data = new NFLData();
  data.json = async url => url.includes('scoreboard')
    ? { events: [{ id: 'live', status: { type: { state: 'in' } }, competitions: [{ competitors: [{ team: { id: '20' } }] }] }] }
    : { header: { id: 'live', competitions: [{ status: { type: { state: 'in' } } }] } };
  data.latestGame = async () => { throw Error('Should not use fallback'); };
  assert.equal((await data.game({ id: 'p', team: { id: '20' } })).eventId, 'live');
});
function confirmation(customId) {
  const edits = [], calls = [];
  return { edits, calls, options: {
    userId: 'owner', player: { id: 'p', displayName: 'Justin Jefferson' }, date: '2025-12-26', suggestion: { date: '2025-12-25', eventId: 'nearest' },
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
  const fixture = confirmation('nfl-date-yes'); await suggestGame(fixture.options);
  assert.deepEqual(fixture.calls, ['ack', 'nearest', 'render']);
  assert.deepEqual(fixture.edits.at(-1).components, []);
});
test('confirmed alternative games use the same flip card presenter', async () => {
  const fixture = confirmation('nfl-date-yes');
  let shown;
  fixture.options.showCard = async (message, model) => { shown = { message, model }; };
  await suggestGame(fixture.options);
  assert.equal(shown.message, fixture.options.reply);
  assert.equal(shown.model.played, true);
  assert.deepEqual(fixture.calls, ['ack', 'nearest']);
});
test('No and timeout remove buttons without loading an alternative', async () => {
  for (const choice of ['nfl-date-no', 'timeout']) {
    const fixture = confirmation(choice); await suggestGame(fixture.options);
    assert.ok(!fixture.calls.includes('nearest'));
    assert.deepEqual(fixture.edits.at(-1).components, []);
  }
});
