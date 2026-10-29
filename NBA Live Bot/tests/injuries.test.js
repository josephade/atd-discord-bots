const { test } = require('node:test');
const assert = require('node:assert/strict');
const { parse } = require('../commands');
const { NBAData } = require('../data');
const { Injuries, parseInjuries, returnInfo, injuryPage, injuriesHTML, PAGE_SIZE, TEAM_PREFIX, NEXT, REFRESH } = require('../injuries');
const date = '2026-10-27T18:00:00Z';
const teams = Array.from({ length: 30 }, (_, i) => ({ id: String(i + 1), displayName: `Team ${String(i + 1).padStart(2, '0')}`, abbreviation: `T${i + 1}` }));
function injury(id, extra = {}) {
  return { id, date, status: 'Out', athlete: { id, displayName: `Player ${id}` },
    shortComment: 'Reported injury.', details: { side: 'Left', type: 'Knee', detail: 'Sprain', returnDate: '2026-11-04' }, ...extra };
}
const feed = () => ({ timestamp: date, status: 'success', injuries: [{ id: '1', displayName: 'Team 01', injuries: Array.from({ length: 10 }, (_, i) => injury(String(i))) }] });
const report = () => parseInjuries(feed(), teams);

test('injury and injuries commands support team shortcuts without a player search', () => {
  assert.deepEqual(parse('NBA INJURY'), { command: 'injury', query: '' });
  assert.deepEqual(parse('nba injuries Lakers'), { command: 'injury', query: 'Lakers' });
  assert.deepEqual(parse('nba injury :lakers:'), { command: 'injury', query: ':lakers:' });
});

test('optional Lakers shortcodes and actual Discord custom emoji open the team directly', async () => {
  const r = report();
  r.teams[0] = { ...r.teams[0], name: 'Los Angeles Lakers', abbreviation: 'LAL' };
  const f = fixture({ data: { injuries: async () => r } });
  for (const query of [':lakers:', '<:Lakers:123456789012345678>', '<a:lakers:123456789012345678>', ':LAL:', 'Lakers']) {
    await f.controller.open(f.message, query);
    assert.equal(f.controller.entries.get('report').teamId, '1', query);
  }
  await f.controller.open(f.message);
  assert.equal(f.controller.entries.get('report').teamId, 'all');
});

test('every source player is preserved and all 30 teams remain selectable', () => {
  const r = report(); assert.equal(r.teams.length, 30);
  assert.equal(r.teams.reduce((n, team) => n + team.players.length, 0), 10);
  assert.equal(r.teams[0].players[0].injury, 'Left Knee · Sprain');
  assert.equal(r.teams.at(-1).players.length, 0);
});

test('provider error cannot be mistaken for a healthy league', () => {
  assert.throws(() => parseInjuries({}, teams), /feed is unavailable/);
  assert.throws(() => parseInjuries({ status: 'error', injuries: [] }, teams), /feed is unavailable/);
  assert.equal(parseInjuries({ injuries: [] }, teams).teams.length, 30);
});

test('expected returns distinguish estimates, missing timetables, season absences and stale dates', () => {
  const value = injury('one');
  assert.equal(returnInfo(value, date).label, '4 Nov 2026');
  assert.match(returnInfo(value, date).note, /not confirmed/);
  assert.equal(returnInfo({ ...value, shortComment: 'Without a return timetable.' }, date).label, 'TBD');
  assert.equal(returnInfo({ ...value, details: { returnDate: '2026-02-30' } }, date).label, 'TBD');
  assert.equal(returnInfo({ ...value, details: {} }, date).label, 'TBD');
  assert.equal(returnInfo({ ...value, details: { fantasyStatus: { abbreviation: 'OFS' }, returnDate: '2027-07-01' } }, date).label, 'Out for season');
  assert.equal(returnInfo({ ...value, details: { returnDate: '2026-10-26' } }, date).label, 'Update pending');
});

test('pagination includes every injured player once and never invents healthy-team entries', () => {
  const r = report(); const first = injuryPage(r), second = injuryPage(r, 'all', 1);
  assert.equal(first.players.length, PAGE_SIZE); assert.equal(second.players.length, 2);
  assert.equal(new Set([...first.players, ...second.players].map(player => player.id)).size, 10);
  assert.equal(injuryPage(r, 'all', 999).page, 1);
  assert.match(injuriesHTML(injuryPage(r, '30')), /No injuries are currently listed by ESPN/);
  assert.throws(() => injuryPage(r, 'unknown'), /no longer available/);
});

test('duplicate source records use the latest report and do not duplicate the player', () => {
  const f = feed();
  f.injuries[0].injuries.push(injury('0', { date: '2026-10-28T10:00Z', status: 'Day-To-Day' }));
  const r = parseInjuries(f, teams);
  assert.equal(r.teams[0].players.length, 10);
  assert.equal(r.teams[0].players.find(player => player.id === '0').status, 'Day-To-Day');
});

test('injury API reads the league directory plus one cached injury feed', async () => {
  const data = new NBAData(), requests = [];
  data.league = async () => ({ teams: teams.map(team => ({ team })) });
  data.json = async (url, ttl) => { requests.push({ url, ttl }); return feed(); };
  assert.equal((await data.injuries()).teams.length, 30);
  assert.match(requests[0].url, /\/injuries\?limit=1000$/); assert.equal(requests[0].ttl, 60000);
});

function fixture(options = {}) {
  const edits = [], notices = []; let loads = 0;
  const message = { id: 'report', edit: async body => edits.push(body) };
  const controller = new Injuries({ data: { injuries: async () => { loads++; return report(); } },
    renderer: { render: async () => Buffer.from('png') }, ...options });
  const interaction = { message, customId: NEXT, values: [], deferUpdate: async () => {}, reply: async body => notices.push(body), followUp: async body => notices.push(body) };
  return { controller, message, interaction, edits, notices, loads: () => loads };
}
test('the menu exposes all 30 teams and All NBA; paging does not refetch or lose names', async () => {
  const f = fixture(); await f.controller.open(f.message);
  const options = f.edits[0].components.slice(0, 2).flatMap(row => row.toJSON().components[0].options);
  assert.equal(options.length, 31); assert.equal(options[0].value, 'all');
  assert.equal(new Set(options.map(option => option.value)).size, 31);
  await f.controller.handle(f.interaction);
  assert.equal(f.controller.entries.get('report').page, 1); assert.equal(f.loads(), 1);
  await f.controller.handle({ ...f.interaction, customId: TEAM_PREFIX + '15', values: ['30'] });
  assert.equal(f.controller.entries.get('report').teamId, '30');
  assert.equal(f.controller.entries.get('report').page, 0); assert.equal(f.loads(), 2);
});

test('refresh preserves selection, failures keep the last report, expired menus are private', async () => {
  let now = 0;
  const f = fixture({ now: () => now, ttl: 10 }); await f.controller.open(f.message, 'T1');
  await f.controller.handle({ ...f.interaction, customId: REFRESH });
  assert.equal(f.controller.entries.get('report').teamId, '1'); assert.equal(f.loads(), 2);
  f.controller.data.injuries = async () => { throw Error('offline'); };
  await f.controller.handle({ ...f.interaction, customId: REFRESH });
  assert.equal(f.edits.length, 2); assert.equal(f.notices.at(-1).flags, 64);
  now = 11; await f.controller.handle(f.interaction); assert.match(f.notices.at(-1).content, /expired/);
});

test('unknown teams cannot trigger unrelated queries and ambiguous shortcuts are rejected', async () => {
  const f = fixture(); await assert.rejects(f.controller.open(f.message, 'Team'), /More than one team/);
  await f.controller.open(f.message);
  await f.controller.handle({ ...f.interaction, customId: TEAM_PREFIX + '0', values: ['999'] });
  assert.equal(f.loads(), 2); assert.equal(f.notices.at(-1).flags, 64);
});

test('unsafe provider text is escaped and photos are limited to ESPN CDN', () => {
  const model = injuryPage(report());
  model.players[0] = { ...model.players[0], name: '<script>', headshot: 'https://bad.test/image', note: '<unsafe>' };
  const markup = injuriesHTML(model);
  assert.match(markup, /&lt;script&gt;/); assert.doesNotMatch(markup, /<script>|https:\/\/bad/);
  assert.match(markup, /EXPECTED RETURN/); assert.match(markup, /Return dates are estimates/);
});
