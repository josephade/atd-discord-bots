const { test } = require('node:test');
const assert = require('node:assert/strict');
const { parse } = require('../commands');
const { NFLData, extractBoxscore } = require('../data');
const { Boxscores, boxscoreHTML, SELECT_PREFIX, HOME_ID, PREV_ID, NEXT_ID, REFRESH_ID, shiftWeek } = require('../boxscores');
const { gamesHTML } = require('../games_layout');

test('boxscores command supports the active slate or a validated date', () => {
  assert.deepEqual(parse('NFL BOXSCORES'), { command: 'boxscores', date: undefined });
  assert.deepEqual(parse('NFL BOXSCORE'), { command: 'boxscores', date: undefined });
  assert.equal(parse('nfl boxscore 2026-10-26').date, '2026-10-26');
  assert.equal(parse('nfl boxscores 2026-10-26').date, '2026-10-26');
  assert.throws(() => parse('nfl boxscores 2026-02-30'), /valid game date/);
  assert.throws(() => parse('nfl boxscores too many arguments'), /Use !nfl boxscores/);
});

test('a dated boxscores request resolves to that date’s NFL week, not just that single day', async () => {
  const data = new NFLData(); const urls = [];
  data.json = async url => { urls.push(url); return { week: { number: 8 }, leagues: [{ season: { year: 2026, type: { type: 2 } } }], events: [
    { id: 'done', status: { type: { state: 'post', shortDetail: 'Final' } } },
    { id: 'next', status: { type: { state: 'pre' } } },
  ] }; };
  assert.equal((await data.games({ all: true, date: '2026-10-26' })).games.length, 2);
  assert.match(urls[0], /dates=20261026/);
  assert.match(urls[1], /week=8&seasontype=2&dates=2026/);
  assert.equal((await data.games()).games.length, 1);
});

const team = (id, homeAway) => ({ team: { id, abbreviation: id, displayName: id + ' Team' }, homeAway, score: '20' });
function summary() {
  return { header: { id: '1', competitions: [{ status: { type: { state: 'post', shortDetail: 'Final' } }, competitors: [team('HOME', 'home'), team('AWAY', 'away')] }] },
    boxscore: { players: [{ team: { id: 'AWAY' }, statistics: [{ name: 'passing', labels: ['C/ATT', 'YDS', 'TD'], keys: ['completions/passingAttempts', 'passingYards', 'passingTouchdowns'], totals: ['30/45', '275', '2'], athletes: [
      { athlete: { id: 'one', displayName: 'Starter' }, starter: true, stats: ['20/30', '225', '2'] },
      { athlete: { id: 'two', displayName: 'Backup' }, stats: ['10/15', '50', '0'] },
      { athlete: { id: 'three', displayName: 'Unavailable' }, didNotPlay: true, reason: 'Injury', stats: [] },
    ] }] }] } };
}
test('full box score keeps both teams, every non-empty category, DNPs, and team totals', () => {
  const model = extractBoxscore(summary());
  assert.equal(model.type, 'boxscore'); assert.equal(model.teams[0].side, 'away');
  assert.equal(model.teams.length, 2); assert.equal(model.teams[0].categories.length, 1);
  assert.equal(model.teams[0].categories[0].key, 'passing');
  assert.equal(model.teams[0].categories[0].players.length, 3);
  assert.equal(model.teams[0].categories[0].players[0].values[1], '225');
  assert.equal(model.teams[0].categories[0].totals[1], '275');
  assert.equal(model.teams[1].categories.length, 0);
  const markup = boxscoreHTML(model);
  assert.match(markup, /DNP · Injury/); assert.match(markup, /TEAM TOTAL/);
  assert.match(markup, /Starter/); assert.match(markup, /Backup/); assert.match(markup, /PASSING/i);
  assert.match(markup, /Player box score unavailable/);
});

test('pregame box score explains pending kickoff and escapes provider text', () => {
  const s = summary(); s.header.competitions[0].status.type.state = 'pre';
  s.boxscore = {};
  s.header.competitions[0].competitors[0].team.displayName = '<unsafe>';
  const markup = boxscoreHTML(extractBoxscore(s));
  assert.match(markup, /Kickoff pending/); assert.match(markup, /&lt;unsafe&gt;/);
  assert.doesNotMatch(markup, /<unsafe>/);
});

// Mirrors ESPN's real scoreboard calendar: ordered week entries within a season-type group.
const calendar = [
  { seasonType: 2, week: 7, label: 'Week 7', detail: 'Oct 21-27' },
  { seasonType: 2, week: 8, label: 'Week 8', detail: 'Oct 28-Nov 3' },
  { seasonType: 2, week: 9, label: 'Week 9', detail: 'Nov 4-10' },
];
function fixture(options = {}) {
  const edits = [], notices = [], calls = [], acks = [];
  const message = { id: 'menu', edit: async body => edits.push(body) };
  const slate = { week: 8, seasonType: 2, year: 2026, label: 'Week 8', detail: 'Oct 28-Nov 3', calendar, games: [
    { id: 'game1', state: 'post', status: 'Final', away: { name: 'Away' }, home: { name: 'Home' } },
    { id: 'game2', state: 'pre', status: 'Scheduled', away: { name: 'Next Away' }, home: { name: 'Next Home' } },
  ] };
  const controller = new Boxscores({ data: { games: async ({ week, seasonType, year }) => ({ ...slate, week, seasonType, year }), boxscore: async id => { calls.push(id); return extractBoxscore(summary()); } },
    renderer: { render: async () => Buffer.from('png') }, ...options });
  const interaction = { message, customId: SELECT_PREFIX + '0', values: ['game1'],
    deferUpdate: async () => acks.push(true), reply: async body => notices.push(body), followUp: async body => notices.push(body) };
  return { controller, message, slate, interaction, edits, notices, calls, acks };
}
test('selecting a game loads only that game and keeps the menu plus All Games button', async () => {
  const f = fixture(); await f.controller.open(f.message, f.slate);
  assert.equal(f.calls.length, 0);
  const menu = f.edits[0].components[0].toJSON().components[0];
  assert.equal(menu.options.length, 2); assert.match(menu.options[0].description, /FINAL/);
  await f.controller.handle(f.interaction);
  assert.deepEqual(f.calls, ['game1']); assert.equal(f.acks.length, 1);
  assert.equal(f.edits.at(-1).files[0].name, 'nfl-boxscore.png');
  assert.equal(f.edits.at(-1).components.at(-1).toJSON().components.at(-1).custom_id, HOME_ID);
  await f.controller.handle({ ...f.interaction, customId: HOME_ID });
  assert.equal(f.edits.at(-1).files[0].name, 'nfl-games.png');
});

test('arbitrary game IDs and expired menus cannot fetch unrelated data', async () => {
  let now = 0; const f = fixture({ now: () => now, ttl: 10 });
  await f.controller.open(f.message, f.slate);
  await f.controller.handle({ ...f.interaction, values: ['not-in-menu'] });
  assert.equal(f.calls.length, 0); assert.equal(f.notices[0].flags, 64);
  now = 11; await f.controller.handle(f.interaction);
  assert.match(f.notices.at(-1).content, /expired/); assert.equal(f.calls.length, 0);
});

test('feed failure leaves the current image intact and lets users retry', async () => {
  const f = fixture(); await f.controller.open(f.message, f.slate);
  f.controller.data.boxscore = async () => { throw Error('feed unavailable'); };
  await f.controller.handle(f.interaction);
  assert.equal(f.edits.length, 1); assert.match(f.notices[0].content, /try Refresh again/);
  f.controller.data.boxscore = async () => extractBoxscore(summary());
  await f.controller.handle(f.interaction);
  assert.equal(f.edits.length, 2);
});

test('menus retain all games when more than 25 options require another row', async () => {
  const f = fixture(); f.slate.games = Array.from({ length: 26 }, (_, i) => ({ ...f.slate.games[0], id: String(i) }));
  await f.controller.open(f.message, f.slate);
  const rows = f.edits[0].components;
  assert.equal(rows.length, 3);
  assert.equal(rows.slice(0, -1).reduce((n, row) => n + row.toJSON().components[0].options.length, 0), 26);
});

test('shiftWeek walks the ordered calendar and holds steady at either boundary', () => {
  assert.equal(shiftWeek(calendar, { seasonType: 2, week: 8 }, -1).week, 7);
  assert.equal(shiftWeek(calendar, { seasonType: 2, week: 8 }, 1).week, 9);
  assert.equal(shiftWeek(calendar, { seasonType: 2, week: 7 }, -1).week, 7);
  assert.equal(shiftWeek(calendar, { seasonType: 2, week: 9 }, 1).week, 9);
});

test('previous/next week navigate the displayed NFL week, including calendar boundaries', async () => {
  const f = fixture(); await f.controller.open(f.message, f.slate);
  const requests = [];
  f.controller.data.games = async args => { requests.push(args); return { ...f.slate, week: args.week, seasonType: args.seasonType, year: args.year, games: [] }; };
  await f.controller.handle({ ...f.interaction, customId: PREV_ID });
  assert.deepEqual(requests[0], { all: true, week: 7, seasonType: 2, year: 2026, refresh: true });
  assert.equal(f.controller.entries.get('menu').slate.week, 7);
  assert.equal(f.edits.at(-1).components.length, 1);
  await f.controller.handle({ ...f.interaction, customId: NEXT_ID });
  assert.equal(requests.at(-1).week, 8);
});

test('the week-boundary Previous/Next button disables rather than looping', async () => {
  const f = fixture(); f.slate.week = 7;
  await f.controller.open(f.message, f.slate);
  const buttons = f.edits[0].components.at(-1).toJSON().components;
  assert.equal(buttons.find(b => b.custom_id === PREV_ID).disabled, true);
  assert.equal(buttons.find(b => b.custom_id === NEXT_ID).disabled, false);
});

test('refresh fetches scores and clock for the same week and preserves selected game', async () => {
  const models = [], requests = [];
  const f = fixture({ renderer: { render: async model => { models.push(model); return Buffer.from('png'); } } });
  await f.controller.open(f.message, f.slate);
  f.controller.data.games = async args => { requests.push(args); return f.slate; };
  await f.controller.handle({ ...f.interaction, customId: REFRESH_ID });
  assert.equal(models.at(-1).type, 'games');
  await f.controller.handle(f.interaction);
  f.controller.data.boxscore = async (id, options) => {
    assert.equal(id, 'game1'); assert.equal(options.refresh, true);
    return { ...extractBoxscore(summary()), status: '2:15 - 4th' };
  };
  await f.controller.handle({ ...f.interaction, customId: REFRESH_ID });
  assert.equal(models.at(-1).status, '2:15 - 4th');
  assert.ok(models.at(-1).updatedAt);
  assert.equal(f.controller.entries.get('menu').selected, 'game1');
  assert.ok(requests.every(r => r.week === 8 && r.seasonType === 2 && r.year === 2026 && r.refresh));
  await f.controller.handle({ ...f.interaction, customId: PREV_ID });
  assert.equal(f.controller.entries.get('menu').selected, null);
});

test('failed navigation retains original week and selected game for retry', async () => {
  const f = fixture(); await f.controller.open(f.message, f.slate);
  await f.controller.handle(f.interaction);
  f.controller.data.games = async () => { throw Error('offline'); };
  await f.controller.handle({ ...f.interaction, customId: PREV_ID });
  assert.equal(f.controller.entries.get('menu').slate.week, 8);
  assert.equal(f.controller.entries.get('menu').selected, 'game1');
});

test('explicit data refresh bypasses a valid cached response', async () => {
  const data = new NFLData(), original = global.fetch;
  let calls = 0;
  global.fetch = async () => ({ ok: true, json: async () => ({ version: ++calls }) });
  try {
    assert.equal((await data.json('https://example.test')).version, 1);
    assert.equal((await data.json('https://example.test')).version, 1);
    assert.equal((await data.json('https://example.test', 0)).version, 2);
  } finally { global.fetch = original; }
});

test('the all-games overview clearly includes final games and their scores', () => {
  const f = fixture(); const markup = gamesHTML({ ...f.slate, allGames: true });
  assert.match(markup, /NFL Box Scores/); assert.match(markup, /FINAL & OTHER GAMES/);
  assert.match(markup, /FINAL/); assert.match(markup, /COMING UP/);
});
