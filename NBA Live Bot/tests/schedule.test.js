const { test } = require('node:test');
const assert = require('node:assert/strict');
const { parse } = require('../commands');
const { NBAData } = require('../data');
const { scheduleHTML } = require('../schedule_layout');

const lakers = { id: '13', displayName: 'Los Angeles Lakers', abbreviation: 'LAL', shortDisplayName: 'Lakers', name: 'Lakers', location: 'Los Angeles', logos: [{ href: 'https://a.espncdn.com/lal.png' }] };
const clippers = { id: '12', displayName: 'LA Clippers', abbreviation: 'LAC', shortDisplayName: 'Clippers', name: 'Clippers', location: 'LA' };
function event(id, state, date, home, us = {}, them = {}) {
  return { id, date, competitions: [{ date, status: { type: { state, name: 'STATUS', shortDetail: state === 'pre' ? 'Scheduled' : 'Final' } },
    broadcasts: [{ media: { shortName: 'ESPN' } }], competitors: [
      { homeAway: home ? 'home' : 'away', team: { id: '13', abbreviation: 'LAL' }, ...us },
      { homeAway: home ? 'away' : 'home', team: { id: '9', displayName: 'Golden State Warriors', abbreviation: 'GS' }, ...them },
    ] }] };
}
function stub() {
  const data = new NBAData(), urls = [];
  data.json = async url => {
    urls.push(url);
    if (url.includes('/teams?')) return { sports: [{ leagues: [{ teams: [{ team: lakers }, { team: clippers }] }] }] };
    return { requestedSeason: { displayName: '2026-27', name: 'Regular Season' }, events: [
      event('next', 'pre', '2026-10-31T02:00Z', false),
      event('won', 'post', '2026-10-22T02:00Z', true, { score: { displayValue: '127' }, winner: true }, { score: { displayValue: '105' }, winner: false }),
      event('lost', 'post', '2026-10-25T02:00Z', false, { score: { displayValue: '99' }, winner: false }, { score: { displayValue: '110' }, winner: true }),
      event('later', 'pre', '2026-11-02T02:00Z', true),
    ] };
  };
  return { data, urls };
}

test('nba schedule parses a team and requires one', () => {
  assert.deepEqual(parse('nba schedule Lakers'), { command: 'schedule', query: 'Lakers' });
  assert.deepEqual(parse('NBA Schedule los angeles lakers'), { command: 'schedule', query: 'los angeles lakers' });
  assert.throws(() => parse('nba schedule'), /Include a team/);
});

test('team lookup matches nickname, abbreviation, emoji name and rejects unknowns', async () => {
  const { data } = stub();
  assert.equal((await data.findTeam('lakers')).id, '13');
  assert.equal((await data.findTeam('LAC')).id, '12');
  assert.equal((await data.findTeam('<:Lakers:123456>')).id, '13');
  await assert.rejects(data.findTeam('Knicks'), /No NBA team matches/);
});

test('schedule splits upcoming games from recent results, newest result first', async () => {
  const { data, urls } = stub();
  const s = await data.schedule('Lakers');
  assert.match(urls.at(-1), /\/teams\/13\/schedule$/);
  assert.equal(s.record, '1-1');
  assert.equal(s.season, '2026-27 Regular Season');
  assert.deepEqual(s.upcoming.map(g => g.id), ['next', 'later']);
  assert.deepEqual(s.recent.map(g => [g.id, g.result, g.score]), [['lost', 'L', '99–110'], ['won', 'W', '127–105']]);
  assert.equal(s.upcoming[0].home, false); assert.equal(s.upcoming[0].opponent.abbreviation, 'GS'); assert.equal(s.upcoming[0].broadcast, 'ESPN');
  const html = scheduleHTML({ ...s, timeZone: 'UTC', updatedAt: '2026-10-29T12:00:00Z' });
  assert.match(html, /Los Angeles Lakers/); assert.match(html, /UPCOMING/); assert.match(html, /LAST RESULTS/); assert.match(html, /99–110/);
});
